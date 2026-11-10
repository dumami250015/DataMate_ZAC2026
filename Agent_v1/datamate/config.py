from pathlib import Path
from typing import Literal
import yaml
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paths(Strict):
    split: str
    model: str = ""
    work: str = "work"


class ModelConfig(Strict):
    backend: Literal["transformers", "vllm"] = "transformers"
    revision: str = "local-content-hash"
    quantization: Literal["none", "nf4"] = "nf4"
    dtype: Literal["float16", "bfloat16", "float32"] = "float16"
    device_map: str = "auto"
    context_tokens: int = Field(16384, ge=2048)
    max_new_tokens: int = Field(3072, ge=128)
    thinking_tokens: int = Field(4096, ge=128)
    thinking: bool = False
    repair_thinking: bool = True
    attention: str = "sdpa"
    tensor_parallel_size: int = Field(1, ge=1)
    gpu_memory_utilization: float = Field(0.85, gt=0, lt=1)


class PipelineConfig(Strict):
    semantic_repairs: int = Field(1, ge=0, le=3)
    verify_with_llm: bool = True
    mapping_with_llm: bool = True
    generated_sql_fallback: bool = True
    max_result_rows: int = Field(1000, ge=1)
    profile_samples: int = Field(6, ge=2, le=20)
    max_entity_candidates: int = Field(30, ge=1)


class RuntimeConfig(Strict):
    seed: int = 42
    sql_timeout_seconds: float = Field(30, gt=0)
    run_deadline_seconds: int = Field(18000, gt=0)
    finalization_reserve_seconds: int = Field(120, ge=0)
    export_every_questions: int = Field(5, ge=1)
    memory_limit: str = "2GB"
    threads: int = Field(2, ge=1)
    resume: bool = True
    fail_fast: bool = False


class Config(Strict):
    paths: Paths
    model: ModelConfig = Field(default_factory=ModelConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @classmethod
    def load(cls, path, **overrides):
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        value.setdefault("paths", {}).update({k: str(v) for k, v in overrides.items() if v is not None})
        return cls.model_validate(value)

    def validate_paths(self, require_model=True):
        root = Path(self.paths.split)
        for name in ["questions.jsonl", "rulebook.md", "calendar.csv", "data"]:
            if not (root / name).exists():
                raise FileNotFoundError(root / name)
        if require_model and not (Path(self.paths.model) / "config.json").is_file():
            raise FileNotFoundError("Set paths.model to a complete local Qwen checkpoint")
        work = Path(self.paths.work).resolve()
        if work == root.resolve() or root.resolve() in work.parents:
            raise ValueError("Work directory must be outside the read-only split")
        work.mkdir(parents=True, exist_ok=True)

