"""Pure-JS safety tests for the small Cowork Markdown renderer."""

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "static" / "js" / "markdown-utils.js"
FIXTURE = ROOT / "tests" / "fixtures" / "cowork-finding-context-refs.txt"


def _node(expression: str) -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    program = source + "\nconsole.log(JSON.stringify(" + expression + "));"
    result = subprocess.run(
        ["node", "-e", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class TestMarkdownUtils:
    def test_script_syntax(self):
        result = subprocess.run(
            ["node", "--check", str(SCRIPT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_strip_context_refs_preserves_labels_and_https(self):
        text = FIXTURE.read_text(encoding="utf-8")
        rendered = _node(f"_stripContextRefs({json.dumps(text)})")

        assert "context:person" not in rendered
        assert "context:meeting" not in rendered
        assert "alice@example.com" in rendered
        assert "Alice/Bob 1:1 (biweekly)" in rendered
        assert "https://example.com/roadmap" in rendered
        assert "https://example.com/doc" in rendered

    def test_render_supports_allowlisted_markdown(self):
        rendered = _node(
            "renderCoworkMarkdown("
            + json.dumps("## Heading\n\n**Bold** and *italic*\n\n- One\n- Two")
            + ")"
        )

        assert "<h2>Heading</h2>" in rendered
        assert "<strong>Bold</strong>" in rendered
        assert "<em>italic</em>" in rendered
        assert "<ul>" in rendered
        assert "<li>One</li>" in rendered

    def test_render_allows_https_links_only(self):
        rendered = _node(
            "renderCoworkMarkdown("
            + json.dumps(
                "[safe](https://example.com/x) [bad](javascript:alert(1))"
            )
            + ")"
        )

        assert 'href="https://example.com/x"' in rendered
        assert "javascript:" not in rendered
        assert "bad" in rendered

    def test_render_escapes_html_and_event_handlers(self):
        rendered = _node(
            "renderCoworkMarkdown("
            + json.dumps('<script>alert(1)</script><img src=x onerror="alert(2)">')
            + ")"
        )

        assert "<script" not in rendered
        assert "<img" not in rendered
        assert "&lt;script&gt;" in rendered
