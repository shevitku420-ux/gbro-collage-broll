#!/usr/bin/env python3
"""ใช้ Gemini API วิเคราะห์ jobs ของ paper-collage workflow.

สคริปต์นี้ทำหน้าที่เป็น planning/orchestration layer ไม่ใช่ video renderer:
- อ่าน omni-jobs.json
- ให้ Gemini แตก prompt เป็น motion groups และชื่อ layer
- เติม timeline, curve และ QA constraints แบบ deterministic
- เขียน normalized motion-plan JSON ต่อ job

การรัน:
    export GEMINI_API_KEY="..."
    python3 gemini_collage_workflow.py
    python3 gemini_collage_workflow.py --jobs omni-jobs.json --project-root /path/to/project

ถ้าไม่มีภาพอ้างอิงใน project-root สคริปต์จะยังสร้างแผนได้ แต่จะติดสถานะ
needs_reference_validation และห้ามถือว่า exact final composition ผ่าน QA.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types


DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
ALLOWED_CURVES = {
    "STATIC_HOLD",
    "SLIDE_EASE_OUT",
    "SOFT_PLACE",
    "SNAP_SETTLE",
    "STAGGERED_ASSEMBLY",
    "SNAP_UPWARD",
    "PATH_REVEAL",
    "GENTLE_APPROACH",
}

SYSTEM_PROMPT = """
คุณเป็นผู้วางแผน motion สำหรับวิดีโอ paper-collage stop-motion ไม่ใช่ผู้สร้างวัตถุใหม่
จงอ่าน job prompt ที่ให้มา แล้วแตกเฉพาะองค์ประกอบที่กล่าวถึงหรือมีอยู่ในภาพปลายทางตามสัญญา
ห้ามเพิ่มวัตถุ บุคคล โลโก้ ข้อความ หรือการเคลื่อนไหวที่ไม่ได้รับอนุญาต

กติกากลาง:
- วิดีโอเป็น one continuous locked-off vertical 9:16 shot ยาว 5 วินาที ไม่มีเสียง
- ลำดับต้องเริ่มจาก empty field และจบด้วย exact completed last frame
- กลุ่มต้องเรียงตามคำว่า first/then/finally ใน prompt
- layer เป็น rigid paper cut-out; opacity=1, scale=1, shadow อยู่กับ layer, morphing=false
- ห้าม camera move, cut, zoom, shake, morphing, fade, scale bounce, squash, stretch และ teleport
- เลือก curve ได้เฉพาะ STATIC_HOLD, SLIDE_EASE_OUT, SOFT_PLACE, SNAP_SETTLE,
  STAGGERED_ASSEMBLY, SNAP_UPWARD, PATH_REVEAL และ GENTLE_APPROACH
- ใช้ PATH_REVEAL เฉพาะ thread/ribbon/เส้น, SNAP_UPWARD เฉพาะ arrow หรือชิ้นที่ระบุว่าพุ่งขึ้น,
  GENTLE_APPROACH เฉพาะมือหรือการเข้าหาอย่างนุ่มนวล
- ห้ามเดาตำแหน่งพิกเซลจากข้อความ ให้ใช้ placeholder reference-x/reference-y

ตอบเป็น JSON object เท่านั้น ไม่ใช้ Markdown และมีโครงสร้าง:
{
  "motion_groups": [
    {
      "name": "ชื่อกลุ่มตาม prompt",
      "order": 1,
      "layers": [
        {"name": "ชื่อ layer", "description": "สิ่งที่อยู่ในภาพ", "curve_id": "SOFT_PLACE"}
      ]
    }
  ],
  "analysis_notes": ["ข้อสังเกตหรือ blocker ที่อ้างจาก prompt"]
}
จงสร้าง 3–5 motion groups และ 1–4 layers ต่อกลุ่ม โดยไม่สร้าง layer ที่ไม่มีเหตุผลจาก prompt
""".strip()


def require_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("ไม่พบ GEMINI_API_KEY โปรดตั้งค่า export GEMINI_API_KEY='...'")
    return key


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Gemini ไม่ได้ส่ง JSON object ที่อ่านได้")
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("ผลลัพธ์จาก Gemini ต้องเป็น JSON object")
    return data


def safe_curve(value: Any, fallback: str) -> str:
    candidate = str(value or "").upper().strip()
    return candidate if candidate in ALLOWED_CURVES else fallback


def curve_for_group(name: str, suggested: Any, order: int) -> str:
    lowered = name.lower()
    if any(word in lowered for word in ("thread", "ribbon", "ด้าย", "ริบบอน", "เส้น")):
        return "PATH_REVEAL"
    if any(word in lowered for word in ("arrow", "ลูกศร", "upward", "พุ่งขึ้น")):
        return "SNAP_UPWARD"
    if any(word in lowered for word in ("hand", "มือ", "gently", "เบา")):
        return "GENTLE_APPROACH"
    defaults = {1: "SLIDE_EASE_OUT", 2: "STAGGERED_ASSEMBLY", 3: "SOFT_PLACE", 4: "SNAP_SETTLE"}
    return safe_curve(suggested, defaults.get(order, "SOFT_PLACE"))


def time_window(order: int, total_groups: int) -> tuple[float, float, float]:
    # Default contract for a five-second collage assembly; preserves empty and final holds.
    windows = {
        1: (0.30, 0.90, 1.10),
        2: (0.98, 1.72, 1.90),
        3: (1.80, 2.95, 3.15),
        4: (3.10, 3.90, 4.08),
    }
    if total_groups <= 4 and order in windows:
        return windows[order]
    middle_start, middle_end = 0.30, 4.08
    slot = (middle_end - middle_start) / max(total_groups, 1)
    start = middle_start + (order - 1) * slot
    settle = min(start + slot * 0.96, middle_end)
    near = start + (settle - start) * 0.78
    return (round(start, 2), round(near, 2), round(settle, 2))


def style_contract(prompt: str) -> dict[str, Any]:
    lowered = prompt.lower()
    background = "unknown-paper-field"
    known_backgrounds = [
        "navy paper field",
        "mustard-yellow paper field",
        "burnt-orange paper field",
        "deep-forest-green paper field",
    ]
    for value in known_backgrounds:
        if value in lowered:
            background = value
            break
    accents: list[str] = []
    for value in ("orange cardstock accents", "teal and red cardstock", "green, blue and red cardstock accents", "rust and ochre cardstock accents"):
        if value in lowered:
            accents.append(value)
    return {
        "background": background,
        "texture": "editorial halftone paper collage",
        "accents": accents or ["cardstock accents from approved last frame"],
        "keylines": "cream",
        "edges": "crisp cut edges",
        "shadows": "soft shadows",
    }


def asset_status(project_root: Path, relative_path: str) -> str:
    path = project_root / relative_path
    return "available" if path.is_file() else "missing"


def analyze_job(client: genai.Client, job: dict[str, Any], model: str) -> dict[str, Any]:
    prompt = f"{SYSTEM_PROMPT}\n\nJOB ID: {job.get('id')}\nJOB PROMPT:\n{job.get('prompt', '')}"
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = parse_json_response(response.text or "{}")
    groups = data.get("motion_groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError(f"job {job.get('id')} ไม่มี motion_groups ที่ใช้ได้")
    return data


def build_plan(job: dict[str, Any], analysis: dict[str, Any], project_root: Path) -> dict[str, Any]:
    raw_groups = analysis.get("motion_groups", [])
    groups: list[dict[str, Any]] = []
    layers: list[dict[str, Any]] = []
    total = len(raw_groups)
    layer_counter = 1
    for index, raw_group in enumerate(raw_groups, start=1):
        if not isinstance(raw_group, dict):
            continue
        name = str(raw_group.get("name") or f"Motion group {index}").strip()
        order = index
        group_id = f"G{index:02d}"
        start, near, settle = time_window(order, total)
        raw_layers = raw_group.get("layers")
        if not isinstance(raw_layers, list) or not raw_layers:
            raw_layers = [{"name": name, "description": name, "curve_id": "SOFT_PLACE"}]
        group_layer_ids: list[str] = []
        for raw_layer in raw_layers[:4]:
            if not isinstance(raw_layer, dict):
                raw_layer = {"name": name, "description": name}
            layer_id = f"L{layer_counter:02d}"
            z_order = layer_counter
            layer_counter += 1
            layer_name = str(raw_layer.get("name") or name).strip()
            description = str(raw_layer.get("description") or layer_name).strip()
            curve = curve_for_group(name, raw_layer.get("curve_id"), order)
            overshoot = 0.0
            if curve == "SNAP_SETTLE":
                overshoot = 0.015
            elif curve == "SNAP_UPWARD":
                overshoot = 0.0
            layer = {
                "layer_id": layer_id,
                "name": layer_name,
                "z_order": z_order,
                "group_id": group_id,
                "time": {
                    "in": start,
                    "near_target": near,
                    "settle": settle,
                    "hold_until": 5.00,
                },
                "start_pose": {
                    "x": "outside",
                    "y": "outside",
                    "rotation_deg": 0.0,
                    "scale": 1.0,
                },
                "target_pose": {
                    "x": "reference-x",
                    "y": "reference-y",
                    "rotation_deg": 0.0,
                    "scale": 1.0,
                },
                "curve": {
                    "id": curve,
                    "overshoot_ratio_max": overshoot,
                    "settle_seconds": round(max(0.08, min(0.15, settle - near)), 2),
                },
                "appearance": {
                    "opacity": 1.0,
                    "shadow_attached": True,
                    "morphing": False,
                },
                "description": description,
                "validation_note": "ตรวจ target position/rotation/scale/shadow จาก approved last frame ก่อน production; ห้ามเดาจากข้อความ",
            }
            layers.append(layer)
            group_layer_ids.append(layer_id)
        groups.append({"id": group_id, "name": name, "order": order, "layers": group_layer_ids})

    first_frame = str((job.get("image") or ["", ""])[0])
    last_frame = str((job.get("image") or ["", ""])[1])
    first_status = asset_status(project_root, first_frame)
    last_status = asset_status(project_root, last_frame)
    blockers: list[str] = []
    if first_status != "available":
        blockers.append(f"missing first frame: {first_frame}")
    if last_status != "available":
        blockers.append(f"missing last frame: {last_frame}")
    if blockers:
        qa_status = "needs_reference_validation"
        still_approval = "blocked"
    else:
        qa_status = "pending"
        still_approval = "pending"

    return {
        "id": job.get("id"),
        "mode": job.get("mode", "short_collage_broll"),
        "image_inputs": {"first_frame": first_frame, "last_frame": last_frame},
        "aspect_ratio": job.get("aspect_ratio", "9:16"),
        "duration_seconds": float(job.get("duration", 5)),
        "audio": False,
        "style_contract": style_contract(str(job.get("prompt", ""))),
        "motion_groups": groups,
        "layers": layers,
        "provider": "gemini-planner",
        "output": job.get("output"),
        "approval": {"concept": "pending", "still": still_approval, "video": "pending"},
        "qa_status": qa_status,
        "blockers": blockers,
        "analysis_notes": analysis.get("analysis_notes", []),
        "execution_note": "Gemini สร้าง motion metadata เท่านั้น; การแยก alpha, compositing, rendering MP4 และการยืนยัน exact last frame ต้องทำด้วย deterministic tools และ QA จากภาพจริง",
    }


def write_report(output_dir: Path, manifest: list[dict[str, Any]]) -> None:
    lines = [
        "# Gemini collage workflow report",
        "",
        "สคริปต์ใช้ Gemini สร้าง motion-plan metadata จาก omni-jobs.json; ยังไม่ได้ render วิดีโอ",
        "",
        "| Job | Plan | QA status | Blockers |",
        "|---|---|---|---|",
    ]
    for item in manifest:
        blockers = "<br>".join(item.get("blockers", [])) or "-"
        lines.append(f"| {item['id']} | `{item['motion_plan']}` | `{item['qa_status']}` | {blockers} |")
    lines.extend([
        "",
        "## Interpretation",
        "",
        "สถานะ `needs_reference_validation` หมายความว่ายังขาดภาพ first frame หรือ last frame ใน project-root จึงยังไม่สามารถยืนยัน target pose, alpha edge หรือ exact final composition ได้ การสร้างวิดีโอจริงต้องหยุดไว้จนกว่าจะเติมภาพและทำ QA จากเฟรมจริง",
        "",
        "Gemini ถูกใช้ในขั้นวิเคราะห์/แตก motion groups/สร้าง metadata เท่านั้น ส่วนการแยกเลเยอร์ alpha, keyframe compositing, rendering MP4 และการตรวจพิกเซลต้องใช้ deterministic tools และผู้ตรวจ",
        "",
    ])
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gemini planner for paper-collage workflow")
    parser.add_argument("--jobs", default="omni-jobs.json", help="ไฟล์ jobs JSON")
    parser.add_argument("--project-root", default=".", help="รากโปรเจกต์ที่ใช้ resolve image paths")
    parser.add_argument("--output-dir", default="gemini_workflow_output", help="โฟลเดอร์ output")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="ชื่อโมเดล Gemini")
    parser.add_argument("--job-id", help="ประมวลผลเฉพาะ job id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        require_api_key()
        jobs_path = Path(args.jobs)
        project_root = Path(args.project_root).resolve()
        jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
        if not isinstance(jobs, list):
            raise ValueError("jobs JSON ต้องเป็น array")
        if args.job_id:
            jobs = [job for job in jobs if job.get("id") == args.job_id]
        if not jobs:
            raise ValueError("ไม่พบ job ที่ต้องประมวลผล")

        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        manifest: list[dict[str, Any]] = []
        for job in jobs:
            job_id = str(job.get("id", "job"))
            print(f"Analyzing {job_id} with {args.model} ...", file=sys.stderr)
            analysis = analyze_job(client, job, args.model)
            plan = build_plan(job, analysis, project_root)
            out_path = output_dir / f"{job_id}.motion-plan.json"
            out_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            manifest.append({
                "id": job_id,
                "motion_plan": str(out_path),
                "qa_status": plan["qa_status"],
                "blockers": plan["blockers"],
            })
            print(f"Wrote {out_path}", file=sys.stderr)
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_report(output_dir, manifest)
        print(f"Wrote {manifest_path}", file=sys.stderr)
        print(f"Wrote {output_dir / 'report.md'}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"เกิดข้อผิดพลาด: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
