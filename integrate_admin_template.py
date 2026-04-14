from pathlib import Path
import re
import shutil


def main() -> None:
    root = Path(__file__).resolve().parent
    build = root / "arabela_admin_panel_template" / "build"
    templates_dir = root / "templates" / "arabela_admin"
    static_dir = root / "static" / "arabela_admin"

    templates_dir.mkdir(parents=True, exist_ok=True)
    static_dir.mkdir(parents=True, exist_ok=True)

    dashboard_html = (build / "index.html").read_text(encoding="utf-8")
    dashboard_html = dashboard_html.replace(
        "eCommerce Dashboard | TailAdmin - Tailwind CSS Admin Dashboard Template",
        "Arabela Admin Dashboard",
    )
    dashboard_html = dashboard_html.replace("TailAdmin", "Arabela Admin")

    dashboard_html = dashboard_html.replace(
        'href="favicon.ico"', 'href="{% static \'arabela_admin/favicon.ico\' %}"'
    )
    dashboard_html = dashboard_html.replace(
        'href="style.css"', 'href="{% static \'arabela_admin/style.css\' %}"'
    )
    dashboard_html = dashboard_html.replace(
        'src="bundle.js"', 'src="{% static \'arabela_admin/bundle.js\' %}"'
    )
    dashboard_html = re.sub(
        r'src="src/images/([^"]+)"',
        r'src="{% static \'arabela_admin/images/\1\' %}"',
        dashboard_html,
    )

    dashboard_html = "{% load static %}\n" + dashboard_html
    (templates_dir / "dashboard.html").write_text(dashboard_html, encoding="utf-8")

    shutil.copy2(build / "style.css", static_dir / "style.css")
    shutil.copy2(build / "bundle.js", static_dir / "bundle.js")
    shutil.copy2(build / "favicon.ico", static_dir / "favicon.ico")
    shutil.copytree(build / "src" / "images", static_dir / "images", dirs_exist_ok=True)


if __name__ == "__main__":
    main()
