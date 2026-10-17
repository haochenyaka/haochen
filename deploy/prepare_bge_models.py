"""准备本地 BGE 模型目录，供 deploy/docker-compose.yml 的 bge-embed / bge-rerank 挂载。

默认从 ModelScope 下载 BAAI/bge-m3 与 BAAI/bge-reranker-v2-m3；bge-m3 官方只提供
pytorch_model.bin，而 TEI 需要 safetensors，因此脚本会做一次性转换。

转换依赖：pip install torch safetensors
用法（仓库根目录）：python deploy/prepare_bge_models.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODELSCOPE = "https://modelscope.cn/models/BAAI/{model}/resolve/master/{name}"
MODEL_FILES = {
    "bge-m3": (
        "config.json",
        "modules.json",
        "pytorch_model.bin",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    ),
    "bge-reranker-v2-m3": (
        "config.json",
        "model.safetensors",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    ),
}
VERIFY_FILES = {
    "bge-m3": (
        "config.json",
        "modules.json",
        "model.safetensors",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    ),
    "bge-reranker-v2-m3": (
        "config.json",
        "model.safetensors",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "tokenizer.json",
        "tokenizer_config.json",
    ),
}
MIN_WEIGHT_BYTES = 2_000_000_000
PRINT_EVERY_BYTES = 32 * 1024 * 1024


def _download(model: str, name: str, dest: Path, *, force: bool) -> None:
    target = dest / name
    if target.exists() and target.stat().st_size > 0 and not force:
        print(f"[skip] {model}/{name} 已存在（{target.stat().st_size / 1024**2:.1f} MiB）")
        return
    part = dest / f"{name}.part"
    start = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "haochen-model-prep/1.0"}
    if start:
        headers["Range"] = f"bytes={start}-"
    url = MODELSCOPE.format(model=model, name=name)
    print(f"[get ] {model}/{name}（从 {start / 1024**2:.1f} MiB 继续）")
    written = start
    total = 0
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=120
        ) as response:
            if start and response.status != 206:
                start = 0
                written = 0
            total = int(response.headers.get("Content-Length") or 0) + start
            next_mark = written + PRINT_EVERY_BYTES
            with part.open("ab" if start else "wb") as handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
                    written += len(chunk)
                    if written >= next_mark:
                        _progress(written, total)
                        next_mark = written + PRINT_EVERY_BYTES
    except urllib.error.HTTPError as exc:
        if exc.code != 416 or not part.exists():
            raise
    _progress(written, total)
    part.replace(target)
    print(f"[done] {model}/{name} -> {target}")


def _progress(written: int, total: int) -> None:
    if total:
        print(f"       {written / 1024**2:.1f} / {total / 1024**2:.1f} MiB")
    else:
        print(f"       {written / 1024**2:.1f} MiB")


def _convert_bge_m3(dest: Path, *, force: bool, keep_bin: bool) -> None:
    source = dest / "bge-m3" / "pytorch_model.bin"
    target = dest / "bge-m3" / "model.safetensors"
    if target.exists() and target.stat().st_size >= MIN_WEIGHT_BYTES and not force:
        print("[skip] bge-m3/model.safetensors 已存在")
        if not keep_bin and source.exists():
            source.unlink()
        return
    try:
        import torch
        from safetensors.torch import save_file
    except ImportError as exc:  # pragma: no cover - 运维脚本，缺依赖时直接提示
        raise SystemExit("转换需要 torch 与 safetensors：pip install torch safetensors") from exc

    print("[conv] bge-m3: pytorch_model.bin -> model.safetensors")
    state = torch.load(source, map_location="cpu", weights_only=True)
    if isinstance(state, dict) and isinstance(state.get("state_dict"), dict):
        state = state["state_dict"]
    tensors = {
        name: tensor.detach().clone().contiguous()
        for name, tensor in state.items()
        if torch.is_tensor(tensor)
    }
    save_file(tensors, str(target), metadata={"format": "pt"})
    print(f"[done] bge-m3/model.safetensors 张量数={len(tensors)}")
    if not keep_bin:
        source.unlink()
        print("[done] 已删除 bge-m3/pytorch_model.bin（TEI 只需要 safetensors）")


def _verify(root: Path) -> None:
    problems: list[str] = []
    for model, files in VERIFY_FILES.items():
        model_dir = root / model
        for name in files:
            path = model_dir / name
            if not path.exists() or path.stat().st_size == 0:
                problems.append(f"缺少 {model}/{name}")
        weights = model_dir / "model.safetensors"
        if weights.exists() and weights.stat().st_size < MIN_WEIGHT_BYTES:
            problems.append(f"{model} 权重体积异常：{weights.stat().st_size} 字节")
        config_path = model_dir / "config.json"
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            print(
                f"[ok  ] {model}: hidden_size={config.get('hidden_size')} "
                f"num_layers={config.get('num_hidden_layers')}"
            )
    if problems:
        raise SystemExit("校验失败：\n- " + "\n- ".join(problems))
    total = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    print(f"[ok  ] 模型目录校验通过：{root}（{total / 1024**3:.2f} GiB）")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parent / "models",
        help="模型输出目录（默认 deploy/models）",
    )
    parser.add_argument("--force", action="store_true", help="重新下载并重新转换")
    parser.add_argument("--verify-only", action="store_true", help="只校验现有目录")
    parser.add_argument(
        "--keep-bin", action="store_true", help="转换后保留 bge-m3/pytorch_model.bin"
    )
    args = parser.parse_args()

    args.dest.mkdir(parents=True, exist_ok=True)
    if not args.verify_only:
        for model, files in MODEL_FILES.items():
            model_dir = args.dest / model
            model_dir.mkdir(parents=True, exist_ok=True)
            for name in files:
                _download(model, name, model_dir, force=args.force)
        _convert_bge_m3(args.dest, force=args.force, keep_bin=args.keep_bin)
    _verify(args.dest)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
