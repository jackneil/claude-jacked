"""The HTML plan template renders its Mermaid diagrams.

The template loads Mermaid with a dynamic import inside a module script. That import resolves after the
window "load" event, so ``startOnLoad: true`` never renders anything (the diagrams stayed as raw source text
in every artifact made from the template, 2026-10-11). The template must call ``mermaid.run()`` itself.
"""

from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "jacked" / "data" / "templates" / "plan-template.html"


def test_the_template_renders_mermaid_explicitly():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "startOnLoad: true" not in text
    assert "startOnLoad: false" in text
    run = text.index("await mermaid.run(")
    assert text.index("mermaid.initialize(") < run < text.index("} catch (err)")
