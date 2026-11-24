"""Render crisp visual PNG previews of the n8n workflow canvas, Tickets tab, and Audit Log tab.

Generated from `workflow/kesar_order_support_copilot_n8n.json`, `outputs/tickets_v2.csv`,
and `outputs/audit_log_v2.csv` so reviewers can inspect the exact architecture and 25-row
outputs directly in `screenshots/`.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOTS = ROOT / "screenshots"


def _font(size: int, bold: bool = False, devanagari: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if devanagari:
        dev_candidates = (
            ["C:/Windows/Fonts/NirmalaB.ttf", "C:/Windows/Fonts/Nirmala.ttc", "C:/Windows/Fonts/mangalb.ttf"]
            if bold
            else ["C:/Windows/Fonts/Nirmala.ttc", "C:/Windows/Fonts/NirmalaS.ttf", "C:/Windows/Fonts/mangal.ttf"]
        )
        for path in dev_candidates:
            if Path(path).exists():
                try:
                    return ImageFont.truetype(path, size=size)
                except OSError:
                    pass
    candidates = (
        ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/consolab.ttf"]
        if bold
        else ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/consola.ttf"]
    )
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                pass
    return ImageFont.load_default()


def _truncate(text: str, max_chars: int) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= max_chars else clean[: max_chars - 1] + "…"


def _wrap_two_lines(text: str, width_chars: int) -> list[str]:
    words = text.split()
    first: list[str] = []
    second: list[str] = []
    for word in words:
        target = first if len(" ".join(first + [word])) <= width_chars and not second else second
        target.append(word)
    if not second:
        return [" ".join(first)]
    return [" ".join(first), _truncate(" ".join(second), width_chars)]


def render_workflow_png() -> Path:
    wf = json.loads((ROOT / "workflow/kesar_order_support_copilot_n8n.json").read_text(encoding="utf-8"))
    width, height = 2960, 1020
    img = Image.new("RGB", (width, height), (248, 250, 252))
    draw = ImageDraw.Draw(img)

    for x in range(40, width, 36):
        for y in range(96, height, 36):
            draw.ellipse((x - 1, y - 1, x + 1, y + 1), fill=(218, 224, 232))

    draw.rectangle((0, 0, width, 76), fill=(24, 32, 48))
    draw.text((36, 16), f"n8n Workflow Blueprint — {wf['name']}", fill=(255, 255, 255), font=_font(25, bold=True))
    draw.text(
        (36, 48),
        f"Rendered from workflow/kesar_order_support_copilot_n8n.json  •  {len(wf['nodes'])} nodes  •  Structured Parser + Auto-Fix + AI Agent Tools + Dedup Sheets  •  Zero secrets",
        fill=(176, 192, 216),
        font=_font(14),
    )

    # Explicit non-overlapping visual layout coordinates on the 2960x1020 canvas
    layout_overrides = {
        "Inbound Email Webhook": (50, 210),
        "Manual Batch Trigger (25 Fixture Emails)": (50, 430),
        "Fetch 25 Test Emails": (245, 430),
        "Split Out Emails": (440, 430),
        "Rate-Limit Guard (3s)": (635, 430),
        "Validate Payload & Privacy Screen": (830, 310),
        "LLM Classify & Extract": (1035, 310),
        "Classifier Chat Model (Temp 0)": (950, 560),
        "Auto-Fixing Output Parser": (1160, 560),
        "Structured JSON Schema (llm_output.schema.json)": (1160, 720),
        "Schema Fallback & Confidence Gate (0.75)": (1240, 310),
        "Fetch Mock Order API (orders.json)": (1445, 310),
        "Verify Ownership & Compute Delivery Days": (1650, 310),
        "Route by Intent": (1855, 310),
        "Return & Complaint Policy Engine": (2060, 460),
        "Lookup Open Ticket by order_id": (2265, 460),
        "Create or Update Support Ticket (Dedup)": (2470, 460),
        "AI Reply Agent (Tools: get_order + search_faq)": (2265, 210),
        "Agent Chat Model": (2060, 660),
        "Tool: get_order": (2265, 660),
        "Tool: search_faq": (2470, 660),
        "Safety Guardrail & Build Audit Row": (2565, 310),
        "Log Every Email to Google Sheet (Audit Log)": (2760, 310),
    }

    nodes = wf["nodes"]
    by_name = {}
    for node in nodes:
        if node["name"] in layout_overrides:
            cx, cy = layout_overrides[node["name"]]
        else:
            px, py = node["position"]
            cx, cy = int(60 + (px - 160) * 0.72), int(120 + (py - 220) * 1.2)
        by_name[node["name"]] = (cx, cy, node)

    node_w, node_h = 176, 74
    for src_name, targets in wf["connections"].items():
        if src_name not in by_name:
            continue
        sx, sy, _ = by_name[src_name]
        for conn_type, branches in targets.items():
            is_ai = conn_type != "main"
            color = (139, 92, 246) if is_ai else (100, 116, 139)
            for branch in branches:
                for edge in branch:
                    dst_name = edge["node"]
                    if dst_name not in by_name:
                        continue
                    dx, dy, _ = by_name[dst_name]
                    if is_ai:
                        start = (sx + node_w // 2, sy)
                        end = (dx + node_w // 2, dy + node_h)
                        mid_y = (start[1] + end[1]) // 2
                        draw.line([start, (start[0], mid_y), (end[0], mid_y), end], fill=color, width=2)
                    else:
                        start = (sx + node_w, sy + node_h // 2)
                        end = (dx, dy + node_h // 2)
                        mid_x = (start[0] + end[0]) // 2
                        draw.line([start, (mid_x, start[1]), (mid_x, end[1]), end], fill=color, width=3)
                    draw.ellipse((end[0] - 4, end[1] - 4, end[0] + 4, end[1] + 4), fill=color)

    f_title = _font(12, bold=True)
    f_sub = _font(10)
    for name, (cx, cy, node) in by_name.items():
        ntype = node["type"]
        if "webhook" in ntype or "Trigger" in ntype:
            accent = (16, 185, 129)
            badge = "TRIGGER"
        elif "langchain.agent" in ntype or "chainLlm" in ntype:
            accent = (234, 88, 12)
            badge = "LLM / AGENT"
        elif "langchain" in ntype:
            accent = (139, 92, 246)
            badge = "MODEL / TOOL / PARSER"
        elif "googleSheets" in ntype:
            accent = (22, 163, 74)
            badge = "GOOGLE SHEETS"
        elif "switch" in ntype:
            accent = (37, 99, 235)
            badge = "ROUTER"
        else:
            accent = (71, 85, 105)
            badge = "CODE / HTTP"

        draw.rounded_rectangle((cx + 2, cy + 3, cx + node_w + 2, cy + node_h + 3), radius=10, fill=(215, 222, 232))
        draw.rounded_rectangle((cx, cy, cx + node_w, cy + node_h), radius=10, fill=(255, 255, 255), outline=accent, width=2)
        draw.rounded_rectangle((cx, cy, cx + 7, cy + node_h), radius=4, fill=accent)
        draw.text((cx + 12, cy + 8), badge, fill=accent, font=f_sub)
        lines = _wrap_two_lines(name, 22)
        draw.text((cx + 12, cy + 26), lines[0], fill=(15, 23, 42), font=f_title)
        if len(lines) > 1:
            draw.text((cx + 12, cy + 44), lines[1], fill=(51, 65, 85), font=f_sub)

    out = SCREENSHOTS / "workflow.png"
    img.save(out)
    return out


def _render_table_png(
    csv_path: Path,
    out_path: Path,
    title: str,
    subtitle: str,
    columns: list[tuple[str, int, int]],
) -> Path:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    row_h = 34
    header_h = 40
    top_h = 78
    pad = 24
    total_w = pad * 2 + sum(col_w for _, col_w, _ in columns)
    total_h = top_h + header_h + len(rows) * row_h + pad

    img = Image.new("RGB", (total_w, total_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, total_w, top_h), fill=(15, 118, 62))
    draw.text((pad, 14), title, fill=(255, 255, 255), font=_font(22, bold=True))
    draw.text((pad, 46), subtitle, fill=(214, 245, 227), font=_font(13))

    y = top_h
    draw.rectangle((pad, y, total_w - pad, y + header_h), fill=(241, 245, 249), outline=(203, 213, 225))
    x = pad
    f_hdr = _font(13, bold=True)
    f_cell = _font(12, devanagari=True)
    for col_name, col_w, _ in columns:
        draw.line([(x, y), (x, total_h - pad)], fill=(226, 232, 240), width=1)
        draw.text((x + 8, y + 11), col_name, fill=(15, 23, 42), font=f_hdr)
        x += col_w
    draw.line([(total_w - pad, y), (total_w - pad, total_h - pad)], fill=(203, 213, 225), width=1)

    y += header_h
    for idx, row in enumerate(rows):
        bg = (255, 255, 255) if idx % 2 == 0 else (248, 250, 252)
        if row.get("needs_human") == "true" or row.get("priority") == "High":
            bg = (254, 249, 239) if idx % 2 == 0 else (254, 243, 226)
        draw.rectangle((pad, y, total_w - pad, y + row_h), fill=bg, outline=(226, 232, 240))
        x = pad
        for col_name, col_w, max_chars in columns:
            val = _truncate(str(row.get(col_name, "")), max_chars)
            color = (30, 41, 59)
            if col_name == "priority" and val == "High":
                color = (185, 28, 28)
            elif col_name == "action_taken" and val == "draft_reply":
                color = (21, 128, 61)
            elif col_name == "needs_human" and val == "true":
                color = (180, 83, 9)
            draw.text((x + 8, y + 9), val, fill=color, font=f_cell)
            draw.line([(x, y), (x, y + row_h)], fill=(226, 232, 240), width=1)
            x += col_w
        y += row_h

    img.save(out_path)
    return out_path


def main() -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    wf_out = render_workflow_png()
    tickets_out = _render_table_png(
        ROOT / "outputs/tickets_v2.csv",
        SCREENSHOTS / "tickets.png",
        "Kesar Support — Tickets Tab (v2 Output Preview)",
        "9 unique deduplicated tickets from 10 return/complaint emails (E04 + E21 merged into TKT-002)  •  Source: outputs/tickets_v2.csv",
        [
            ("ticket_id", 95, 10),
            ("order_id", 95, 10),
            ("email_ids", 95, 12),
            ("intent", 125, 16),
            ("priority", 85, 10),
            ("status", 75, 10),
            ("customer_email", 215, 28),
            ("summary", 280, 38),
            ("notes", 580, 82),
        ],
    )
    audit_out = _render_table_png(
        ROOT / "outputs/audit_log_v2.csv",
        SCREENSHOTS / "audit_log.png",
        "Kesar Support — Audit Log Tab (All 25 Processed Emails, v2)",
        "Scenario date: 2026-11-20  •  7 draft_reply, 10 ticket_created (9 unique), 4 needs_human, 4 log_only  •  Source: outputs/audit_log_v2.csv",
        [
            ("email_id", 75, 8),
            ("from_email", 205, 27),
            ("intent", 120, 15),
            ("secondary_intent", 125, 15),
            ("order_id", 85, 10),
            ("language", 78, 9),
            ("sentiment", 85, 10),
            ("confidence", 85, 8),
            ("order_found", 92, 8),
            ("action_taken", 120, 16),
            ("needs_human", 98, 8),
            ("ticket_id", 88, 10),
            ("reply_draft", 460, 62),
        ],
    )
    print(json.dumps({
        "workflow": str(wf_out.relative_to(ROOT)),
        "tickets": str(tickets_out.relative_to(ROOT)),
        "audit_log": str(audit_out.relative_to(ROOT)),
    }, indent=2))


if __name__ == "__main__":
    main()
