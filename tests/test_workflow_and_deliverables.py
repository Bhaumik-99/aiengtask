import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkflowAndDeliverableTests(unittest.TestCase):
    def test_n8n_workflow_json_is_valid_connected_and_has_agent_tools(self):
        wf_path = ROOT / "workflow/kesar_order_support_copilot_n8n.json"
        self.assertTrue(wf_path.is_file(), "n8n workflow JSON must exist in workflow/")
        wf = json.loads(wf_path.read_text(encoding="utf-8"))
        node_names = {node["name"] for node in wf["nodes"]}
        self.assertGreaterEqual(len(node_names), 15)

        # Every connection endpoint must reference a real node in wf['nodes'].
        for src, conn_map in wf["connections"].items():
            self.assertIn(src, node_names, f"Unknown source node: {src}")
            for branches in conn_map.values():
                for branch in branches:
                    for edge in branch:
                        self.assertIn(edge["node"], node_names, f"Unknown target node: {edge['node']}")

        # Verify AI Agent node has at least two attached tools (`get_order` and `search_faq`).
        tool_nodes = [n for n in wf["nodes"] if n["type"] == "@n8n/n8n-nodes-langchain.toolCode"]
        tool_names = {n["parameters"]["name"] for n in tool_nodes}
        self.assertEqual(tool_names, {"get_order", "search_faq"})

        # Verify Auto-fixing + Structured Output Parser are present.
        node_types = {n["type"] for n in wf["nodes"]}
        self.assertIn("@n8n/n8n-nodes-langchain.agent", node_types)
        self.assertIn("@n8n/n8n-nodes-langchain.outputParserAutofixing", node_types)
        self.assertIn("@n8n/n8n-nodes-langchain.outputParserStructured", node_types)
        self.assertIn("n8n-nodes-base.googleSheets", node_types)

    def test_no_secrets_or_api_keys_committed_in_repo(self):
        secret_pattern = re.compile(r"(sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z\-_]{30,}|gsk_[A-Za-z0-9]{20,})")
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {".git", "__pycache__", ".venv"} for part in path.parts):
                continue
            if path.name == ".env" or path.name.startswith(".env.") and path.name != ".env.example":
                continue
            if path.suffix.lower() in {".png", ".jpg", ".sqlite3"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(secret_pattern.search(text), f"Potential secret found in {path}")

    def test_required_screenshots_exist_and_are_valid_png_files(self):
        for name in ("workflow.png", "tickets.png", "audit_log.png"):
            img_path = ROOT / "screenshots" / name
            self.assertTrue(img_path.is_file(), f"Missing screenshot: {name}")
            header = img_path.read_bytes()[:8]
            self.assertEqual(header, b"\x89PNG\r\n\x1a\n", f"Invalid PNG header in {name}")

    def test_product_thinking_answers_each_within_150_words(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        section = readme.split("## 7. Product-thinking questions")[1].split("## 8. How I used AI tools")[0]
        blocks = re.split(r"### \d+\. [^\n]+\n", section)[1:]
        self.assertEqual(len(blocks), 5, "Expected 5 product-thinking answers in README.md")
        for idx, block in enumerate(blocks, start=1):
            words = re.findall(r"\S+", block.strip())
            self.assertLessEqual(len(words), 150, f"Product-thinking Q{idx} exceeds 150 words ({len(words)} words)")

    def test_test_report_contains_all_25_email_rows(self):
        report = (ROOT / "TEST_REPORT.md").read_text(encoding="utf-8")
        for num in range(1, 26):
            email_id = f"E{num:02d}"
            self.assertIn(f"| {email_id} |", report, f"Missing {email_id} row in TEST_REPORT.md")


if __name__ == "__main__":
    unittest.main(verbosity=2)

