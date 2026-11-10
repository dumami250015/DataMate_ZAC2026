from __future__ import annotations
import os
import time
from pathlib import Path
from .util import dumps, loads, digest, read_json


class GenerationError(RuntimeError):
    pass


def extract_json(text):
    # Discard reasoning only when its explicit closing delimiter exists.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    elif "<think>" in text:
        raise GenerationError("Unfinished thinking output")
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        value = loads(text)
    except ValueError as exc:
        raise GenerationError("Model did not emit one complete JSON object") from exc
    if not isinstance(value, dict):
        raise GenerationError("Expected JSON object")
    return value


class Backend:
    def structured(self, system, payload, schema, thinking=False, deadline=None):
        schema_text = dumps(schema.model_json_schema())
        messages = [{"role": "system", "content": system + "\nJSON schema:\n" + schema_text},
                    {"role": "user", "content": dumps(payload)}]
        text = self.generate(messages, thinking=thinking, deadline=deadline)
        return schema.model_validate(extract_json(text))

    def smoke_test(self, deadline=None):
        from .contracts import Review
        value = self.structured("Return verdict accept, issues [], evidence [].", {}, Review, deadline=deadline)
        if value.verdict != "accept":
            raise GenerationError("Model smoke test failed")


class TransformersBackend(Backend):
    def __init__(self, path, config, seed=42, checkpoint_hash=""):
        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
            os.environ[key] = "1"
        import torch
        import transformers
        from transformers import AutoTokenizer, BitsAndBytesConfig, set_seed
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA GPU required for full Qwen inference; use prepare --raw-only for CPU inspection")
        set_seed(seed)
        self.torch, self.config = torch, config
        dtype = getattr(torch, config.dtype)
        if config.dtype == "bfloat16" and not torch.cuda.is_bf16_supported():
            raise RuntimeError("Selected GPU does not support BF16; select float16 explicitly")
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        settings = dict(local_files_only=True, trust_remote_code=False, dtype=dtype,
                        device_map=config.device_map, attn_implementation=config.attention,
                        output_loading_info=True)
        if config.quantization == "nf4":
            settings["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=dtype)
        model_config = read_json(Path(path) / "config.json")
        # Load the full official checkpoint without ad-hoc key rewriting; text prompts only.
        if "vision_config" in model_config:
            loader = transformers.AutoModelForImageTextToText
        else:
            loader = transformers.AutoModelForCausalLM
        self.model, info = loader.from_pretrained(path, **settings)
        if info.get("missing_keys") or info.get("mismatched_keys") or info.get("error_msgs"):
            raise RuntimeError(f"Checkpoint did not load completely: {info}")
        self.loading_info = info
        self.model.eval()
        self.identity = digest({"backend": "transformers", "checkpoint": checkpoint_hash, "config": config.model_dump(), "transformers": transformers.__version__, "torch": torch.__version__})
        self.last_stats = {}

    def generate(self, messages, thinking=False, deadline=None):
        if deadline:
            deadline.check()
        torch = self.torch
        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=thinking)
        inputs = self.tokenizer(text, return_tensors="pt", add_special_tokens=False)
        budget = self.config.thinking_tokens if thinking else self.config.max_new_tokens
        count = inputs["input_ids"].shape[-1]
        if count + budget > self.config.context_tokens:
            raise GenerationError(f"Context budget exceeded: input={count}, output={budget}, limit={self.config.context_tokens}. Increase tested context or reduce catalog samples; no silent truncation.")
        device = self.model.get_input_embeddings().weight.device
        inputs = {k: v.to(device) for k, v in inputs.items()}
        start = time.monotonic()
        kwargs = {"max_new_tokens": budget, "do_sample": False, "use_cache": True,
                  "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id}
        if deadline:
            kwargs["max_time"] = deadline.remaining
        with torch.inference_mode():
            output = self.model.generate(**inputs, **kwargs)
        generated = output[0, count:]
        self.last_stats = {"input_tokens": count, "output_tokens": len(generated), "elapsed": time.monotonic() - start,
                           "peak_cuda_bytes": torch.cuda.max_memory_allocated()}
        if len(generated) >= budget:
            raise GenerationError("Generation reached token limit; result rejected as potentially truncated")
        if deadline:
            deadline.check()
        text = self.tokenizer.decode(generated, skip_special_tokens=False).strip()
        # Preserve thinking delimiters; skip_special_tokens can remove them in some tokenizer revisions.
        endings = {self.tokenizer.eos_token, self.tokenizer.pad_token, "<|im_end|>", "<|endoftext|>"} - {None, ""}
        changed = True
        while changed:
            changed = False
            for ending in endings:
                if text.endswith(ending):
                    text = text[:-len(ending)].rstrip()
                    changed = True
        return text


class VLLMBackend(Backend):
    def __init__(self, path, config, seed=42, checkpoint_hash=""):
        os.environ["HF_HUB_OFFLINE"] = "1"
        from vllm import LLM
        if config.quantization == "nf4":
            raise ValueError("Use Transformers for on-the-fly NF4; vLLM profile requires validated checkpoint quantization")
        self.config = config
        self.model = LLM(model=path, tokenizer=path, trust_remote_code=False, dtype=config.dtype,
                         tensor_parallel_size=config.tensor_parallel_size, max_model_len=config.context_tokens,
                         gpu_memory_utilization=config.gpu_memory_utilization, seed=seed)
        self.tokenizer = self.model.get_tokenizer()
        self.identity = digest({"backend": "vllm", "checkpoint": checkpoint_hash, "config": config.model_dump()})
        self.last_stats = {}

    def generate(self, messages, thinking=False, deadline=None):
        from vllm import SamplingParams
        if deadline:
            deadline.check()
        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=thinking)
        budget = self.config.thinking_tokens if thinking else self.config.max_new_tokens
        if len(self.tokenizer.encode(text)) + budget > self.config.context_tokens:
            raise GenerationError("Context budget exceeded")
        result = self.model.generate([text], SamplingParams(temperature=0, max_tokens=budget), use_tqdm=False)[0].outputs[0]
        if deadline:
            deadline.check()
        if result.finish_reason == "length":
            raise GenerationError("Truncated vLLM generation")
        return result.text


def load_backend(path, config, seed=42, checkpoint_hash=""):
    cls = TransformersBackend if config.backend == "transformers" else VLLMBackend
    return cls(path, config, seed, checkpoint_hash)

