#!/usr/bin/env python3
"""
genpic —— Runpod Serverless 生图客户端

两个工作流（都基于 ComfyUI API 格式图，跑在 runpod-workers/worker-comfyui 上）：

  anime  日系动漫画风   Anima (2B, CircleStone Labs + Comfy Org)
                       纯文生图 / 图生图（图生图由 denoise 控制重绘强度）

  qwen   通用生图      Qwen-Image-2.1 (7B, 阿里)
                       0 张图 = 文生图；1~10 张图 = 多图编辑（prompt 里用 <image1>..<image10> 指代）

默认只打印将要发送的请求体（dry-run），加 --send 才真正发出去。

用法
----
  # 看一眼请求长什么样
  python genpic.py anime --prompt "1girl, solo, white hair"

  # 真发（需要环境变量 RUNPOD_API_KEY）
  python genpic.py anime --endpoint-id <ID> --prompt "1girl, solo, white hair" --send

  # 动漫图生图
  python genpic.py anime --endpoint-id <ID> --prompt "..." --image ref.png --denoise 0.6 --send

  # Qwen 纯文生图
  python genpic.py qwen --endpoint-id <ID> --prompt "..." --send

  # Qwen 多图编辑（1~10 张）
  python genpic.py qwen --endpoint-id <ID> --prompt "put the logo from <image2> on the bag in <image1>" ^
                        --image a.png --image b.png --send

只依赖标准库 +（发送时）urllib，Windows / PowerShell / bash 都能跑。
"""

import argparse
import base64
import json
import mimetypes
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKFLOWS = HERE / "workflows"

ANIMA_NEG = "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia"


# --------------------------------------------------------------------------- utils

def load_workflow(name: str) -> dict:
    path = WORKFLOWS / f"{name}.json"
    if not path.exists():
        sys.exit(f"找不到工作流文件: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def encode_image(path: str) -> dict:
    """读图 -> Runpod input.images 需要的 {name, image} 结构"""
    p = Path(path)
    if not p.exists():
        sys.exit(f"图片不存在: {p}")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    # Runpod 请求体上限：/run 10MB、/runsync 20MB，这里提前拦一道
    mb = len(b64) / 1048576
    if mb > 9.0:
        sys.exit(f"图片过大（base64 后 {mb:.1f}MB），/run 限制 10MB。请先压缩或改用 /runsync。")
    return {"name": f"input_{p.stem}.png", "image": f"data:{mime};base64,{b64}"}


def set_seed(graph: dict, seed: int) -> dict:
    for node in graph.values():
        if node.get("class_type") == "KSampler":
            node["inputs"]["seed"] = seed
    return graph


# --------------------------------------------------------------------------- anime

def build_anima(args) -> dict:
    img2i = bool(args.image)
    graph = load_workflow("anime_img2img" if img2i else "anime_t2i")

    # 模型切换
    graph["1"]["inputs"]["unet_name"] = args.model

    # 提示词
    for node in graph.values():
        ct = node.get("class_type")
        if ct == "CLIPTextEncode":
            title = node.get("_meta", {}).get("title", "")
            if "Negative" in title:
                node["inputs"]["text"] = args.negative or ANIMA_NEG
            else:
                node["inputs"]["text"] = args.prompt

    # 采样
    if img2i:
        graph["4"]["inputs"]["image"] = args.image
        for node in graph.values():
            if node.get("class_type") == "EmptyLatentImage":
                node["inputs"].pop("width", None)
                node["inputs"].pop("height", None)
    else:
        for node in graph.values():
            if node.get("class_type") == "EmptyLatentImage":
                node["inputs"]["width"] = args.width
                node["inputs"]["height"] = args.height

    for node in graph.values():
        if node.get("class_type") == "KSampler":
            node["inputs"]["steps"] = args.steps if args.steps is not None else (8 if args.turbo else 30)
            node["inputs"]["cfg"] = args.cfg if args.cfg is not None else (1.0 if args.turbo else 4.0)
            if img2i:
                node["inputs"]["denoise"] = args.denoise

    # Turbo LoRA：官方 anima-turbo-lora-v0.2（8 步 / CFG 1）
    if args.turbo:
        lora_id = str(max(int(k) for k in graph) + 1)
        graph[lora_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "lora_name": args.turbo_lora,
                "strength_model": 1.0,
                "model": ["1", 0],
            },
            "_meta": {"title": "Anima Turbo LoRA"},
        }
        for node in graph.values():
            if node.get("class_type") == "KSampler":
                node["inputs"]["model"] = [lora_id, 0]

    return {"input": {"workflow": set_seed(graph, args.seed)}}


# --------------------------------------------------------------------------- qwen

def build_qwen(args) -> dict:
    imgs = args.image or []
    if imgs and len(imgs) > 10:
        sys.exit(f"Qwen-Image-2.1 最多 10 张参考图，收到 {len(imgs)} 张")

    graph = load_workflow("qwen21_multi_edit" if imgs else "qwen21_t2i")
    graph["1"]["inputs"]["unet_name"] = args.model

    for node in graph.values():
        if node.get("class_type") == "TextEncodeQwenImage21":
            node["inputs"]["prompt"] = args.prompt
            node["inputs"]["negative_prompt"] = args.negative or ""
            node["inputs"]["resolution"] = args.resolution
        elif node.get("class_type") == "EmptyLatentImage":
            node["inputs"]["width"] = args.width
            node["inputs"]["height"] = args.height
        elif node.get("class_type") == "KSampler":
            node["inputs"]["steps"] = args.steps if args.steps is not None else 25
            node["inputs"]["cfg"] = args.cfg if args.cfg is not None else 1.0

    payload = {"input": {"workflow": set_seed(graph, args.seed)}}

    if imgs:
        # 模板自带 2 个 LoadImage 节点（id 10/11）。按实际图片数重建：
        # 多退少补，然后把每个 LoadImage 指向 input.images 里的文件名。
        template_ids = sorted(
            int(k) for k, v in graph.items() if v.get("class_type") == "LoadImage"
        )
        max_id = max(int(k) for k in graph)
        loader_ids = template_ids[: len(imgs)]
        loader_ids += [max_id + i for i in range(1, len(imgs) - len(loader_ids) + 1)]

        # 删掉用不到的 LoadImage，否则 worker 会去找不存在的文件
        keep = {str(i) for i in loader_ids}
        for node_id in list(graph):
            if graph[node_id].get("class_type") == "LoadImage" and node_id not in keep:
                del graph[node_id]

        images = []
        for i, (node_id, path) in enumerate(zip(loader_ids, imgs), start=1):
            if str(node_id) not in graph:
                # 模板自带的 LoadImage 不够用，补一个
                graph[str(node_id)] = {
                    "class_type": "LoadImage",
                    "inputs": {"image": "", "upload": "image"},
                    "_meta": {"title": f"Load Image {i}"},
                }
            item = encode_image(path)
            graph[str(node_id)]["inputs"]["image"] = item["name"]
            images.append(item)

        for node in graph.values():
            if node.get("class_type") == "TextEncodeQwenImage21":
                # 残留的 images.image_N 先清掉，再按实际数量挂
                for key in [k for k in node["inputs"] if k.startswith("images.image_")]:
                    del node["inputs"][key]
                for i, node_id in enumerate(loader_ids, start=1):
                    node["inputs"][f"images.image_{i}"] = [str(node_id), 0]

        payload["input"]["images"] = images
    return payload


# --------------------------------------------------------------------------- send

def send(endpoint_id: str, payload: dict, timeout: int) -> int:
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        sys.exit("缺少环境变量 RUNPOD_API_KEY（https://console.runpod.io/user/settings）")
    url = f"https://api.runpod.ai/v2/{endpoint_id}/runsync"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:800]}")
    except urllib.error.URLError as e:
        sys.exit(f"请求失败: {e}")

    print(f"\n[{time.time() - t0:.1f}s] status={body.get('status')} "
          f"executionTime={body.get('executionTime')}ms")

    out = body.get("output") or {}
    for err in out.get("errors") or []:
        print(f"  warn: {err}")

    imgs = out.get("images") or []
    if not imgs:
        print(json.dumps(body, ensure_ascii=False)[:2000])
        return 1

    outdir = HERE / "outputs"
    outdir.mkdir(exist_ok=True)
    for i, im in enumerate(imgs):
        if im.get("type") == "base64":
            p = outdir / f"{body.get('id', 'job')}_{i}.png"
            p.write_bytes(base64.b64decode(im["data"]))
            print(f"  saved: {p}")
        else:
            print(f"  {im.get('filename')}: {im.get('data')}")
    return 0


# --------------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Runpod Serverless 生图客户端（Anima / Qwen-Image-2.1）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = ap.add_subparsers(dest="which", required=True)

    a = sub.add_parser("anime", help="日系动漫画风（Anima）")
    a.add_argument("--prompt", required=True)
    a.add_argument("--negative", default="")
    a.add_argument("--image", help="给了就走图生图（img2img）")
    a.add_argument("--width", type=int, default=1024)
    a.add_argument("--height", type=int, default=1024)
    a.add_argument("--steps", type=int)
    a.add_argument("--cfg", type=float)
    a.add_argument("--denoise", type=float, default=0.6, help="图生图重绘强度 0~1")
    a.add_argument("--model", default="anima-base-v1.0.safetensors")
    a.add_argument("--turbo", action="store_true", help="接官方 turbo LoRA（8 步 / CFG 1）")
    a.add_argument("--turbo-lora", default="anima-turbo-lora-v0.2.safetensors")
    a.add_argument("--seed", type=int, default=lambda: secrets.randbits(53))
    a.add_argument("--endpoint-id", default="")
    a.add_argument("--send", action="store_true")
    a.add_argument("--timeout", type=int, default=600)

    q = sub.add_parser("qwen", help="通用生图 / 多图编辑（Qwen-Image-2.1）")
    q.add_argument("--prompt", required=True)
    q.add_argument("--negative", default="")
    q.add_argument("--image", action="append", help="可重复 1~10 次；有图=编辑，无图=文生图")
    q.add_argument("--width", type=int, default=1024)
    q.add_argument("--height", type=int, default=1024)
    q.add_argument("--resolution", type=int, default=1024,
                   help="参考图预处理分辨率预算（512/1024/2048），不是输出尺寸")
    q.add_argument("--steps", type=int, default=25)
    q.add_argument("--cfg", type=float, default=1.0)
    q.add_argument("--model", default="qwen_image_2.1_int8_convrot.safetensors")
    q.add_argument("--seed", type=int, default=lambda: secrets.randbits(53))
    q.add_argument("--endpoint-id", default="")
    q.add_argument("--send", action="store_true")
    q.add_argument("--timeout", type=int, default=600)

    args = ap.parse_args()
    payload = build_anima(args) if args.which == "anime" else build_qwen(args)

    if not args.send:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("\n(dry-run —— 加 --send 并提供 --endpoint-id 才会真的发出去)", file=sys.stderr)
        return 0

    if not args.endpoint_id:
        sys.exit("需要 --endpoint-id")
    return send(args.endpoint_id, payload, args.timeout)


if __name__ == "__main__":
    sys.exit(main())