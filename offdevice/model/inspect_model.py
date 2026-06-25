"""
Load weiser/30M-0.4, prove it runs on the laptop, and report the facts the host
track needs:

  1. A clean load + one reference forward pass (coherent next-token logits).
  2. The resolved GPT-2 config, echoed for comparison against the expected values.
  3. GPT-2 BPE token IDs for candidate benign/anomalous label words -- REPORT only;
     the single-token-vs-logprob choice is made later, so don't hard-code it here.

Run (from repo root, .venv active, after pip install -r offdevice/requirements-model.txt):
    python -m offdevice.model.inspect_model

First run downloads ~60 MB of weights to the HF cache; later runs are offline-fast.
No GPU required (auto-uses cuda if present).
"""


from __future__ import annotations

import platform

import torch
import transformers
from transformers import AutoModelForCausalLM, AutoTokenizer

MODEL_ID = "weiser/30M-0.4"

# A trivial English continuation -- a coherent greedy next token proves the
# pretrained weights loaded (not random init).
SANITY_PROMPT = "The capital of France is"

# Candidate label words. GPT-2 BPE is whitespace-sensitive: "benign" and " benign"
# tokenize differently and mid-sentence words carry the leading space, so report
# both. Short alternatives are included as likely single-token label candidates.
LABEL_CANDIDATES: tuple[str, ...] = (
    "benign", " benign", "Benign", " Benign",
    "anomalous", " anomalous", "Anomalous", " Anomalous",
    " safe", " bad", " good", " evil", " clean", " malware",
    " yes", " no", " 0", " 1",
)


def _device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


def load_tokenizer() -> AutoTokenizer:
    """Load the model's tokenizer, falling back to stock GPT-2 BPE.

    Some TinyLLM checkpoints ship weights only; the GPT-2 vocab (50257 merges) is
    identical either way, so the reported label IDs are the same. Prints the source.
    """
    try:
        tok = AutoTokenizer.from_pretrained(MODEL_ID)
        print(f"  tokenizer    from {MODEL_ID}")
        return tok
    except (OSError, ValueError) as exc:
        print(f"  tokenizer    {MODEL_ID} has none ({type(exc).__name__}); using stock 'gpt2'")
        return AutoTokenizer.from_pretrained("gpt2")


def report_versions(device: str) -> None:
    print("=== environment ===")
    print(f"  python       {platform.python_version()}")
    print(f"  torch        {torch.__version__}")
    print(f"  transformers {transformers.__version__}")
    print(f"  device       {device}", end="")
    if device == "cuda":
        print(f"  ({torch.cuda.get_device_name(0)})")
    else:
        print()
    print()


def report_config(model: AutoModelForCausalLM) -> None:
    """Echo the engine-critical config fields against the expected GPT-2 values."""
    cfg = model.config
    expected = {
        "model_type": "gpt2",
        "n_layer": 6,
        "n_head": 6,
        "n_embd": 384,
        "n_ctx": 1024,
        "n_positions": 1024,
        "vocab_size": 50257,
        "activation_function": "gelu_new",
        "layer_norm_epsilon": 1e-5,
        "tie_word_embeddings": True,
        "bos_token_id": 50256,
        "eos_token_id": 50256,
    }
    print("=== resolved config (compare to expected) ===")
    n_params = sum(p.numel() for p in model.parameters())
    print(f"  total parameters   {n_params:,}  (~{n_params / 1e6:.1f}M)")
    print(f"  weight dtype       {next(model.parameters()).dtype}")
    for key, want in expected.items():
        got = getattr(cfg, key, "<missing>")
        flag = "ok" if got == want else "MISMATCH"
        print(f"  {key:22} {str(got):14} (expect {want!s:14}) {flag}")
    # tied embeddings: out projection should BE the input embedding matrix
    tied = model.get_output_embeddings().weight is model.get_input_embeddings().weight
    print(f"  embeddings tied?   {tied}  (expect True)")
    print()


def reference_forward_pass(
    model: AutoModelForCausalLM, tokenizer: AutoTokenizer, device: str
) -> None:
    """One forward pass over SANITY_PROMPT; print the greedy next token + top-5."""
    enc = tokenizer(SANITY_PROMPT, return_tensors="pt").to(device)
    with torch.no_grad():
        logits = model(**enc).logits  # [1, seq_len, vocab]

    n_tokens = enc["input_ids"].shape[1]
    print("=== reference forward pass ===")
    print(f"  prompt        {SANITY_PROMPT!r}")
    print(f"  input tokens  {n_tokens}")
    print(f"  logits shape  {tuple(logits.shape)}  (expect [1, {n_tokens}, 50257])")

    next_logits = logits[0, -1]
    greedy_id = int(next_logits.argmax())
    print(f"  greedy next   id={greedy_id} -> {tokenizer.decode([greedy_id])!r}")
    top = torch.topk(next_logits, k=5)
    pretty = [(int(i), tokenizer.decode([int(i)])) for i in top.indices]
    print(f"  top-5 next    {pretty}")
    print()


def report_label_tokens(tokenizer: AutoTokenizer) -> None:
    """Report GPT-2 BPE tokenization of each label candidate."""
    print("=== label-word tokenization (report only) ===")
    print("  (single-token candidates are the cheapest to greedy-decode)")
    for word in LABEL_CANDIDATES:
        ids = tokenizer.encode(word, add_special_tokens=False)
        pieces = [tokenizer.decode([i]) for i in ids]
        kind = "single-token" if len(ids) == 1 else f"{len(ids)} tokens"
        print(f"  {word!r:14} -> ids={ids}  pieces={pieces}  [{kind}]")
    print()


def main() -> None:
    device = _device()
    report_versions(device)

    print(f"loading {MODEL_ID} ...")
    tokenizer = load_tokenizer()
    # Checkpoint is bfloat16; newer transformers loads in that dtype rather than
    # upcasting. Force float32 for a deterministic reference pass (.float() dodges
    # the torch_dtype kwarg churn). The host engine pins inference dtype separately.
    model = AutoModelForCausalLM.from_pretrained(MODEL_ID).float()
    model.to(device).eval()

    report_config(model)
    reference_forward_pass(model, tokenizer, device)
    report_label_tokens(tokenizer)
    print("OK -- model loaded, forward pass ran, label tokens reported.")


if __name__ == "__main__":
    main()
