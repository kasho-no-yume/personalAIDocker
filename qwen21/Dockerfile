# genpic — qwen endpoint
#
# 通用生图 / 多图编辑 · Qwen-Image-2.1 7B (阿里, 2026-09-20 发布)
#
# 一个权重同时做：文生图 + 编辑 + 最多 10 张参考图 + 透明 PNG + 原生 2K
#
# 为什么必须升级 ComfyUI：
#   基础镜像是 v0.34.0 (2026-08-25)，早于 Qwen-Image-2.1 发布 (2026-09-20)。
#   v0.34.0 只有 TextEncodeQwenImageEdit / ...EditPlus（那是 2025 年的
#   Qwen-Image-Edit / Edit-2509 用的），没有 2.1 的节点。
#   Qwen-Image-2.1 节点 (TextEncodeQwenImage21 / QwenImage21Cache) 在
#   v0.40.0 里才有 —— 已在容器里实测确认。
#
#   注意：不能用 `comfy update`，它内部跑 git pull，而镜像是 detached HEAD
#   （HEAD detached at v0.34.0），git pull 必然失败。必须显式 fetch + checkout。
#
# 为什么用 INT8 ConvRot 而不是官方 BF16：
#   BF16 权重 31.6GB -> 放不进 24GB 卡
#   INT8 ConvRot 权重 16.1GB -> 24GB 稳
#   官方 ComfyUI 模板默认就是 INT8 ConvRot 组合，精度损失很小。
#
# RUNPOD BUILD 注意：本镜像约 56 GB，构建需下载 16.1 GB 权重。
# Runpod 的 docker build 步骤上限 30 分钟 —— 如果超时，见下方 W4A8 开关。

FROM runpod/worker-comfyui:5.10.0-base

ENV HF_HUB_ENABLE_HF_TRANSFER=1

# ---- handler 同步校验 ----
# 基础镜像自带 Runpod 官方 handler：/handler.py，由 /start.sh（镜像的 CMD）启动，
# 本镜像直接继承，运行时用的就是它。
# 仓库根目录另放了一份字节相同的 handler.py，目的是让 Runpod GitHub 集成的仓库扫描
# 能识别到 handler —— 否则控制台会提示
# "Could not find runpod.serverless.start() in your repo"。
# 下面这行保证镜像里那份和仓库里那份不会悄悄走偏；基础镜像换 tag 后这里会失败，
# 届时把新的 /handler.py 重新拷进仓库根目录即可。
RUN test "$(sha256sum /handler.py | cut -d' ' -f1)" = \
      "c43fcc2bfff6cbb60555d7839b902c54de8a9a55ba20caba2bbd3adf9c2886ec" \
      || { echo "CHANGED: /handler.py —— 基础镜像的 handler 变了，请同步仓库根目录的 handler.py"; exit 1; }

# W4A8=1 时用更小的文本编码器（6.41GB vs 8.71GB），权重总量降到 13.6GB。
# 构建超时或显存吃紧时可以打开。
ARG W4A8=0

# ---- 升级 ComfyUI 到 v0.40.0（Qwen-Image-2.1 节点在这里才有）----
#
# 换代码必须同时补 Python 依赖！
# 5.10.0 基础镜像的 site-packages 是按 v0.34.0 装的，而 v0.40.0 抬了这些 pin：
#   comfy-aimdo  0.4.15 -> 0.5.6      ← storage 子模块是新版才有的
#   comfy-kitchen 0.2.31 -> 0.2.37
#   comfyui-frontend-package / workflow-templates / embedded-docs 也都抬了
# 而 comfy/storage.py 顶层无条件 import comfy_aimdo.storage，所以只 checkout 不装
# 依赖的后果（2026-10-16 实测踩到）：
#   ModuleNotFoundError: No module named 'comfy_aimdo.storage'
#   worker 状态却是 IDLE、unhealthy=0、日志里也有过 "worker is ready"，
#   只有一发 job 才暴露：ComfyUI server (127.0.0.1:8188) not reachable after multiple retries.
# torch / torchvision 用 -c 锁成镜像里现有的 CUDA 版本，别让 pip 顺手换掉。
# （v0.40.0 的 requirements 全是版本 pin 或已满足，实测只会升级上面那几个包，不动 torch。）
RUN set -eux; \
    cd /comfyui; \
    git fetch --tags --force origin; \
    git checkout -q v0.40.0; \
    git log -1 --format='%h %ci'; \
    grep __version__ /comfyui/comfyui_version.py; \
    python3 -c 'import torch; print("torch runtime", torch.__version__)'; \
    { python3 -m pip show torch | awk '/^Version:/{print "torch==" $2}'; \
      python3 -m pip show torchvision | awk '/^Version:/{print "torchvision==" $2}'; } > /tmp/keep.txt; \
    cat /tmp/keep.txt; \
    python3 -m pip install --no-cache-dir -r requirements.txt -c /tmp/keep.txt

# ---- 7B Single-Stream DiT，INT8 ConvRot ----
RUN comfy model download \
      --url https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/diffusion_models/qwen_image_2.1_int8_convrot.safetensors \
      --relative-path models/diffusion_models \
      --filename qwen_image_2.1_int8_convrot.safetensors

# ---- Qwen3-VL-8B 文本编码器 ----
RUN if [ "$W4A8" = "1" ]; then \
      comfy model download \
        --url https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_w4a8.safetensors \
        --relative-path models/text_encoders \
        --filename qwen3vl_8b_w4a8.safetensors; \
    else \
      comfy model download \
        --url https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors \
        --relative-path models/text_encoders \
        --filename qwen3vl_8b_int8_convrot.safetensors; \
    fi

# ---- VAE ----
RUN comfy model download \
      --url https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/vae/qwen_image_2.1_vae_bf16.safetensors \
      --relative-path models/vae \
      --filename qwen_image_2.1_vae_bf16.safetensors

# ---- 自检：权重 + 节点。节点缺失直接构建失败，别等上线才发现 ----
RUN set -eux; \
    test -s /comfyui/models/diffusion_models/qwen_image_2.1_int8_convrot.safetensors \
      || { echo "MISSING: diffusion model"; exit 1; }; \
    if [ "$W4A8" = "1" ]; then \
      test -s /comfyui/models/text_encoders/qwen3vl_8b_w4a8.safetensors \
        || { echo "MISSING: text encoder (w4a8)"; exit 1; }; \
    else \
      test -s /comfyui/models/text_encoders/qwen3vl_8b_int8_convrot.safetensors \
        || { echo "MISSING: text encoder (int8)"; exit 1; }; \
    fi; \
    test -s /comfyui/models/vae/qwen_image_2.1_vae_bf16.safetensors \
      || { echo "MISSING: vae"; exit 1; }; \
    ls -lh /comfyui/models/diffusion_models/ /comfyui/models/text_encoders/ /comfyui/models/vae/; \
    echo "--- 节点检查 ---"; \
    grep -rq "TextEncodeQwenImage21" /comfyui/comfy_extras/ \
      || { echo "MISSING NODE: TextEncodeQwenImage21 —— ComfyUI 版本不对"; exit 1; }; \
    grep -rq "QwenImage21Cache" /comfyui/comfy_extras/ \
      || { echo "MISSING NODE: QwenImage21Cache"; exit 1; }; \
    echo "--- 依赖导入检查（这一步能挡住「只换代码不装依赖」的坑）---"; \
    cd /comfyui && python3 -c "import comfy.storage, comfy.utils; print('comfy.storage / comfy.utils 导入 OK')" \
      || { echo "MISSING DEP: comfy.storage 导不进来 —— 多半是 comfy-aimdo 没升到 v0.40.0 要的版本"; exit 1; }; \
    python3 -c "import comfy_aimdo, importlib.metadata as m; print('comfy-aimdo', m.version('comfy-aimdo'))"; \
    echo "自检通过"