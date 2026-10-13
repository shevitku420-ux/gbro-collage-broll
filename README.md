# gbro-collage-broll

<p align="center">
  <img src="assets/demo-purple.gif" width="180" alt="深紫底：多人协作压出科幻胶片">
  <img src="assets/demo-yellow.gif" width="180" alt="芥末黄底：错误被印刷机批量放大">
  <img src="assets/demo-red.gif" width="180" alt="红底：导演之手摆放棋盘走位">
  <img src="assets/demo-teal.gif" width="180" alt="青绿底：剪刀裁开镜头轨道">
</p>

把一句约 5 秒的口播文稿，压成一个 sharp visual idea，再生成高级编辑风**半调纸拼贴（halftone paper-collage）组装动画** B-roll。

Turn a ~5s voiceover line into a premium editorial paper-collage assemble-from-empty B-roll clip, powered by Gemini Omni Flash first/last-frame video generation.

## 效果

- 强烈平坦的纯色纸面色场 + 黑白 halftone 照片剪贴 + 彩色卡纸点缀
- 元素从空场逐件滑入、卡位、组装（stop-motion 质感），不是淡入或慢 zoom
- 默认交付 9:16、5 秒、720×1280、24fps、无声 MP4，可直接垫在口播下面

## 工作流：三闸门审批

这个 skill 的核心不是 prompt 模板，而是强制的三阶段审批，让你把注意力花在审美判断上，而不是烧生成费用：

1. **Gate 1 · 隐喻确认** — 只输出视觉隐喻方案（核心意思 / 关键物件 / 底色 / 组装顺序），不生成任何图片视频
2. **Gate 2 · 静帧确认** — 确认后才生成彩色拼贴静帧 + contact sheet，再次等你确认
3. **Gate 3 · 视频生成** — 静帧通过后自动用 `gemini-omni-flash-preview` 做首尾帧组装动画，附完整 QA（逐秒抽帧、首帧空场验证、尾帧对照）

批量模式下支持部分通过：只有确认过的条目进入下一阶段。

## 环境要求

首次触发时 skill 会自动运行 `scripts/check_setup.sh` 自检，并针对缺失项给出配置指引。需要：

| 依赖 | 说明 |
|------|------|
| Codex 环境 | Gate 2 静帧生成依赖内置 `image_gen` 工具 |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) 创建，视频生成按量计费 |
| Python >= 3.10 | 用于视频生成脚本 |
| `google-genai >= 2.10.0` | skill 会引导创建共享 venv `~/hyperframes-projects/.omni-venv/` |
| ffmpeg / ffprobe | 首尾帧处理、去音轨、contact sheet |

视频生成脚本（`scripts/generate_video.py` + `scripts/upload_file.py`）已随 skill 自带，无需额外安装其他 skill。

## 安装

把整个目录放进你的 agent skills 目录（例如 `~/.agents/skills/` 或 `~/.claude/skills/`）：

```bash
git clone https://github.com/pyang5166/gbro-collage-broll.git ~/.agents/skills/gbro-collage-broll
```

## 使用

对你的 agent 说：

```text
collage b-roll：很多人以为 AI 是来替你思考的，其实它更像一面镜子，会把你问题里的漏洞照出来。
```

触发词：`collage b-roll`、`纸拼贴 b-roll`、`半调拼贴`、`拼贴风格配画面`、`gbro-collage-broll`。

然后按 Gate 1 → Gate 2 → Gate 3 逐步确认即可。批量给多句文稿也可以，每句一个隐喻一条成片。

## 目录结构

```text
gbro-collage-broll/
├── SKILL.md                        # skill 主文档（三闸门协议 + prompt 模板 + QA 标准）
├── agents/openai.yaml              # Codex interface 配置
├── evals/evals.json                # 四条闸门行为评测
└── scripts/
    ├── check_setup.sh              # 首次使用环境自检
    ├── generate_video.py           # Gemini Omni Flash 批量视频生成
    ├── upload_file.py              # Files API 上传辅助
    └── generate_veo_first_last.py  # 旧 Veo 链路（仅兼容保留，默认不用）
```

## Gemini motion-planning integration

This repository now includes an optional planning stage at `scripts/plan_motion.py`. It uses the Google Gen AI SDK and the `GEMINI_API_KEY` environment variable to turn each approved job prompt into normalized motion-plan JSON: motion groups, layer names, five-second timing, approved curve IDs, and QA blockers. It does **not** render a video and it does not bypass the three approval gates.

The planner deliberately uses `reference-x` and `reference-y` until the real first/last frame files are present. This prevents the model from inventing pixel positions and keeps the exact-last-frame requirement honest. Missing images produce `needs_reference_validation`; only after Gate 2 approval and reference images are available should the existing Omni Flash generator be run.

Example:

```bash
export GEMINI_API_KEY="YOUR_API_KEY"
python3 scripts/plan_motion.py \\
  --jobs omni-jobs.example.json \\
  --project-root . \\
  --output-dir out/motion-plans
```

The command writes one `*.motion-plan.json` per job, plus `manifest.json` and `report.md`. After the stills and references pass approval, create the production batch JSON with the two image paths and run the existing video pipeline:

```bash
python3 scripts/generate_video.py \\
  --batch omni-jobs.json \\
  --concurrency 3
```

The active video path remains `gemini-omni-flash-preview`, with audio stripping and frame QA handled by the existing workflow. The planner is an analysis/metadata layer; alpha-layer extraction, compositing, MP4 rendering, and pixel-level last-frame verification remain deterministic post-processing and QA work.

### Local mock run (no Gemini video request)

For a safe smoke test, `generate_video.py` also supports `--mock`. This path uses local `ffmpeg` only, does not upload images, does not create a Gemini Interaction, and does not require `GEMINI_API_KEY`:

```bash
python3 scripts/generate_video.py \\
  --batch examples/mock-jobs.json \\
  --mock \\
  --concurrency 2
```

The example writes four 5-second, 720×1280, 24fps, silent MP4 files under `out/mock-video/`. They are deterministic placeholders for checking batch execution, output paths, and media validation; they are not model-generated artwork and must never be used as production deliverables.

## FAQ

**为什么强制两次人工确认？**
错误的隐喻或静帧直接进视频生成，浪费的是真金白银的 API 费用。Gate 1 改文字是免费的，Gate 2 重生一张图远比重跑一条视频便宜。

**成片首帧边缘露出一点纸片？**
轻微的可以接受；严格空场需求建议用可编辑时间线的动画工具补前段。

**能换视频模型吗？**
默认固定 `gemini-omni-flash-preview`，明确指定其他模型时才切换。
