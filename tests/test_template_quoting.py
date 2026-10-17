"""Buttons must not break their own onclick attribute.

`{{ x|tojson }}` outputs "double quotes". Inside onclick="..." that closes the attribute early, the
browser sees broken JavaScript and the button silently does nothing (this broke Reports > View Photo,
the Client Access copy buttons and the Logs detail click). Inside double-quoted attributes always use
`{{ x|tojson|forceescape }}` (turns " into &#34;, which the browser turns back into " for the script)."""
import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
ATTR = re.compile(r'\s(on\w+|href)="([^"]*)"', re.S)


def test_tojson_inside_double_quoted_attributes_is_escaped():
    bad = []
    for path in TEMPLATES.glob("*.html"):
        src = path.read_text(encoding="utf-8")
        for m in ATTR.finditer(src):
            for expr in re.findall(r"\{\{(.*?)\}\}", m.group(2), re.S):
                if "tojson" in expr and "forceescape" not in expr:
                    line = src[:m.start()].count("\n") + 1
                    bad.append(f"{path.name}:{line}: {m.group(1)}=... {{{{{expr.strip()}}}}}")
    assert not bad, "Use |tojson|forceescape inside double-quoted attributes:\n" + "\n".join(bad)


def test_view_photo_button_renders_valid_onclick():
    from jinja2 import Environment
    env = Environment(autoescape=True)
    t = env.from_string('<button onclick="openProofPhotoModal({{ (\'/uploads/\' ~ p)|tojson|forceescape }}, '
                        '{{ c|tojson|forceescape }})">x</button>')
    html = t.render(p="proof_1.jpg", c='MH"DG<003>')
    onclick = re.search(r'onclick="([^"]*)"', html).group(1)
    assert onclick.startswith("openProofPhotoModal(") and onclick.endswith(")")   # attribute not cut short
    assert "&#34;" in onclick
