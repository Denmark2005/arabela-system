"""The size guide the gown page opens from its "Size Guide" link.

One table per category, kept here as plain data (not in the database) so the numbers are easy to read, check against a tape measure,
and change in one place. A category with no table has no Size Guide link at all -- the link used to be there on every gown and did
nothing. To give another category a guide, add its entry to SIZE_GUIDES below; the page, the animation and the tests follow it.

Suit, Wedding Gown, Filipiniana, Dresses and Barong have one so far. The sizes run S, M, L, XL, XXL like the size buttons on the gown
page. Each measurement is written exactly as the shop's chart prints it (centimetres first, inches after), so nothing is converted or
rounded here. The page shows only the table, and a category's columns are its own (a suit lists chest, shoulder, sleeve and waist; a
wedding gown lists bust, waist and hips)."""

SUIT_GUIDE = {
    "title": "Suit size guide",
    "columns": ["Chest / Bust", "Shoulder Width", "Sleeve Length", "Waist"],
    "rows": [
        {"code": "S", "values": [("92–96 cm", '(36–38")'), ("44–45 cm", '(17.3–17.7")'), ("60–61 cm", '(23.5–24")'), ("76–80 cm", '(30–31.5")')]},
        {"code": "M", "values": [("97–101 cm", '(38–40")'), ("46–47 cm", '(18.1–18.5")'), ("62–63 cm", '(24.5–25")'), ("81–85 cm", '(32–33.5")')]},
        {"code": "L", "values": [("102–106 cm", '(40–42")'), ("48–49 cm", '(18.9–19.3")'), ("64–65 cm", '(25.5–26")'), ("86–90 cm", '(34–35.5")')]},
        {"code": "XL", "values": [("107–111 cm", '(42–44")'), ("50–51 cm", '(19.7–20.1")'), ("66–67 cm", '(26.5–27")'), ("91–95 cm", '(36–37.5")')]},
        {"code": "XXL", "values": [("112–116 cm", '(44–46")'), ("52–53 cm", '(20.5–20.9")'), ("68–69 cm", '(27.5–28")'), ("96–100 cm", '(38–39.5")')]},
    ],
}

WEDDING_GUIDE = {
    "title": "Wedding gown size guide",
    "columns": ["Bust", "Waist", "Hips"],
    "rows": [
        {"code": "S", "values": [("85–88 cm", '(33.5–34.5")'), ("66–69 cm", '(26–27")'), ("90–93 cm", '(35.5–36.5")')]},
        {"code": "M", "values": [("89–93 cm", '(35–36.5")'), ("70–74 cm", '(27.5–29")'), ("94–98 cm", '(37–38.5")')]},
        {"code": "L", "values": [("94–99 cm", '(37–39")'), ("75–80 cm", '(29.5–31.5")'), ("99–104 cm", '(39–41")')]},
        {"code": "XL", "values": [("100–105 cm", '(39.5–41.5")'), ("81–86 cm", '(32–34")'), ("105–110 cm", '(41.5–43.5")')]},
        {"code": "XXL", "values": [("106–112 cm", '(42–44")'), ("87–93 cm", '(34.5–36.5")'), ("111–116 cm", '(44–45.5")')]},
    ],
}


def _bust_waist_hips_guide(title):
    """The owner's Bust / Waist / Hips chart. Wedding Gown, Filipiniana and Dresses were given the same numbers; each category gets its OWN
    copy (a new dict per call), so changing one category's numbers later never changes another's."""
    return {
        "title": title,
        "columns": ["Bust", "Waist", "Hips"],
        "rows": [
            {"code": "S", "values": [("85–88 cm", '(33.5–34.5")'), ("66–69 cm", '(26–27")'), ("90–93 cm", '(35.5–36.5")')]},
            {"code": "M", "values": [("89–93 cm", '(35–36.5")'), ("70–74 cm", '(27.5–29")'), ("94–98 cm", '(37–38.5")')]},
            {"code": "L", "values": [("94–99 cm", '(37–39")'), ("75–80 cm", '(29.5–31.5")'), ("99–104 cm", '(39–41")')]},
            {"code": "XL", "values": [("100–105 cm", '(39.5–41.5")'), ("81–86 cm", '(32–34")'), ("105–110 cm", '(41.5–43.5")')]},
            {"code": "XXL", "values": [("106–112 cm", '(42–44")'), ("87–93 cm", '(34.5–36.5")'), ("111–116 cm", '(44–45.5")')]},
        ],
    }


FILIPINIANA_GUIDE = _bust_waist_hips_guide("Filipiniana size guide")
DRESSES_GUIDE = _bust_waist_hips_guide("Dresses size guide")

BARONG_GUIDE = {
    "title": "Barong size guide",
    "columns": ["Chest / Bust", "Shoulder Width", "Sleeve Length", "Length"],
    "rows": [
        {"code": "S", "values": [("96–100 cm", '(38–39.5")'), ("43–44 cm", '(17–17.5")'), ("58–59 cm", '(23–23.5")'), ("71–73 cm", '(28–28.5")')]},
        {"code": "M", "values": [("101–105 cm", '(40–41.5")'), ("45–46 cm", '(17.5–18")'), ("60–61 cm", '(23.5–24")'), ("74–76 cm", '(29–29.5")')]},
        {"code": "L", "values": [("106–110 cm", '(42–43.5")'), ("47–48 cm", '(18.5–19")'), ("62–63 cm", '(24.5–25")'), ("77–79 cm", '(30–31")')]},
        {"code": "XL", "values": [("111–115 cm", '(44–45.5")'), ("49–50 cm", '(19.5–20")'), ("64–65 cm", '(25.5–26")'), ("80–82 cm", '(31.5–32")')]},
        {"code": "XXL", "values": [("116–120 cm", '(46–47.5")'), ("51–52 cm", '(20.5–21")'), ("66–67 cm", '(26.5–27")'), ("83–85 cm", '(32.5–33.5")')]},
    ],
}

# Keyed by the category's name as the shop writes it (Gown.category), not by its web address.
SIZE_GUIDES = {
    "Suit": SUIT_GUIDE,
    "Wedding Gown": WEDDING_GUIDE,
    "Filipiniana": FILIPINIANA_GUIDE,
    "Dresses": DRESSES_GUIDE,
    "Barong": BARONG_GUIDE,
}


def size_guide_for(category_label, size_choices):
    """The guide to draw for a gown of this category, or None when there is none to show.

    `size_choices` is what the gown page draws (gowns.views._size_choices): each size with `offered` -- True only for a size the shop
    really has for this gown. A gown that comes in Free Size only has nothing to look up in a S-XXL chart, so it gets no guide. The table
    itself is the same for every gown of the category: it lists every size, whether or not this gown comes in it."""
    guide = SIZE_GUIDES.get(category_label)
    if not guide:
        return None
    offered = {choice["code"] for choice in size_choices if choice.get("offered")}
    if not offered.intersection(row["code"] for row in guide["rows"]):
        return None
    rows = [
        {"code": row["code"], "values": [{"cm": cm, "inch": inch} for cm, inch in row["values"]]}
        for row in guide["rows"]
    ]
    return {"title": guide["title"], "columns": guide["columns"], "rows": rows}
