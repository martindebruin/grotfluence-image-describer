# Handoff: Grötblogg Fine-tuned Vision Model

This document tells you everything you need to know to continue working on Martin's oatmeal blog project in a new session.

---

## What Was Built

Martin runs a Swedish porridge blog at **https://grötfluence.se** with 550+ WordPress posts. Each post has a photo of a bowl of porridge, tagged with the porridge type (category) and toppings (tags).

We fine-tuned **Qwen2.5-VL-3B-Instruct** (a vision-language model) using LoRA on ~586 real blog images augmented to ~6400 training samples. The model learned to look at a photo of porridge and describe what it sees in Swedish, matching Martin's own tagging vocabulary.

**Example output from the model:**
> "Detta är havregröt (variant: kallgröt, lingon) med fiberberikad, frukost och lingon."

The fine-tuned model is now **live in production**, replacing the generic Qwen2.5-VL on the server.

---

## Infrastructure

**Machine:** `frmwrk-ai` — AMD Ryzen AI MAX+ 395, 64 GiB RAM, AMD Radeon 8060S (16 GiB VRAM), ROCm, Fedora 43.

**Models disk:** `/mnt/models/` (NOT `/models/` — common mistake)

**The fine-tuned model is served by llama.cpp as an OpenAI-compatible HTTP API:**

| Port | Service | Model |
|------|---------|-------|
| `:8081` | `llama-server-vision.service` | Fine-tuned Qwen2.5-VL-3B (the one we trained) |
| `:8080` | `llama-server.service` | mistral-small-3.1-24b (unrelated, leave alone) |
| `:8082` | `llama-vision-proxy.service` | WebP→JPEG proxy (leave alone) |

**Model files:**
- Active: `/mnt/models/blobs/qwen2.5vl-3b-unsloth-Q4_K_M.gguf` (fine-tuned)
- mmproj: `/mnt/models/blobs/qwen2.5vl-3b-mmproj-BF16.gguf`
- Backup of original: `/mnt/models/blobs/qwen2.5vl-3b-unsloth-Q4_K_M.gguf.bak`

To roll back: `sudo systemctl stop llama-server-vision && sudo cp /mnt/models/blobs/qwen2.5vl-3b-unsloth-Q4_K_M.gguf.bak /mnt/models/blobs/qwen2.5vl-3b-unsloth-Q4_K_M.gguf && sudo systemctl start llama-server-vision`

---

## How to Call the Model

The model is accessible as an OpenAI-compatible API on `http://localhost:8081`. To analyze a porridge image:

```python
import requests, base64

def analyze_porridge_image(image_path: str) -> str:
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    payload = {
        "model": "qwen2.5vl:3b-gpu",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
                    },
                    {
                        "type": "text",
                        "text": "Analysera bilden och beskriv exakt vilken grötsort och vilka toppings/tillbehör som syns."
                    }
                ]
            }
        ],
        "max_tokens": 200,
        "temperature": 0.1
    }

    response = requests.post("http://localhost:8081/v1/chat/completions", json=payload)
    return response.json()["choices"][0]["message"]["content"]
```

**What it returns:** A Swedish sentence like:
- `"Detta är havregröt med lingonsylt och banan."`
- `"Detta är havregröt (variant: kallgröt) med fiberberikad, frukost och blåbär."`

The format is: `"Detta är {category} [med {topping1}, {topping2}, ...]."` — matching Martin's own WordPress tag/category vocabulary.

---

## WordPress Integration Possibilities

Martin uses WordPress REST API. His credentials are in `/home/martin/claude/training/.env`:
```
WP_BASE_URL=https://grötfluence.se
WP_USER=martin
WP_APP_PASSWORD=<in .env file>
```

**Potential uses for the fine-tuned model:**

1. **Auto-tagging new posts** — when Martin uploads a new porridge photo, call the model to suggest categories and tags before publishing.

2. **Batch verification** — check existing posts where the model's description doesn't match the actual tags (find mislabeled posts).

3. **Alt-text generation** — generate Swedish alt-text for all porridge images for accessibility/SEO.

4. **Search/filtering** — index posts by visual content (e.g. "show me all posts with lingonberry topping").

The WordPress REST API endpoint to update post tags/categories:
```
PATCH /wp-json/wp/v2/posts/{id}
Body: {"tags": [id1, id2], "categories": [id3]}
```
Tags and categories need numeric IDs. Use `GET /wp-json/wp/v2/tags?search=lingonsylt` to resolve name→id.

---

## Training Pipeline (for future retraining)

All scripts are in `/home/martin/claude/training/`. Run with the venv: `/mnt/models/grot-finetune/venv/bin/python3`.

**CRITICAL for ROCm:** Always prefix training commands with `HSA_OVERRIDE_GFX_VERSION=11.0.0`.

```
step1_scrape.py      — Scrape WP blog → metadata.csv + raw images
step2_augment.py     — Augment images 10x (586 → 6446)
step3_dataset.py     — Build train/val JSONL in Qwen chat format
step4_train.py       — LoRA fine-tuning (~3.5 hours on Radeon 8060S)
step5a_merge.py      — Merge LoRA into base model (run with HSA_OVERRIDE)
step5_export_gguf.sh — Convert to GGUF + quantize (uses /home/martin/llama.cpp)
```

To retrain from scratch:
```bash
cd /home/martin/claude/training
source /mnt/models/grot-finetune/venv/bin/activate   # or prefix with venv python
python3 step1_scrape.py          # re-scrapes blog (resumes, skips existing images)
python3 step2_augment.py
python3 step3_dataset.py         # review label output for typos before continuing
HSA_OVERRIDE_GFX_VERSION=11.0.0 python3 step4_train.py
HSA_OVERRIDE_GFX_VERSION=11.0.0 python3 step5a_merge.py
bash step5_export_gguf.sh        # will prompt before hot-swap
```

**Known gotchas (already solved in the scripts):**
- Images must be PIL-resized to MAX_PIXELS=128*28*28 before processor — `max_pixels` kwarg doesn't work reliably for tall/wide images
- `model.enable_input_require_grads()` must be called after `get_peft_model()` for gradient checkpointing to work
- `eval_strategy="no"` during training — eval OOMs on 16 GiB VRAM without gradient checkpointing
- Merge must run on GPU (`device_map="auto"`), not CPU — CPU segfaults with ROCm PyTorch

---

## Quick Health Check

```bash
# Is the service running?
systemctl status llama-server-vision

# Quick inference test
curl -s http://localhost:8081/v1/models | python3 -m json.tool

# Test with a real image
python3 -c "
import requests, base64
img = base64.b64encode(open('/mnt/models/grot-finetune/data/raw/$(ls /mnt/models/grot-finetune/data/raw/ | head -1)', 'rb').read()).decode()
r = requests.post('http://localhost:8081/v1/chat/completions', json={'model':'qwen2.5vl:3b-gpu','messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+img}},{'type':'text','text':'Analysera bilden och beskriv exakt vilken grötsort och vilka toppings/tillbehör som syns.'}]}],'max_tokens':100})
print(r.json()['choices'][0]['message']['content'])
"
```

---

## Summary

The fine-tuned model is live, accessible at `http://localhost:8081`, and responds to the Swedish porridge analysis prompt. The next logical step is building a WordPress integration — either a Python script that Martin runs on demand to auto-tag new posts, or a webhook/plugin that triggers on new post upload.
