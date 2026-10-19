"""The size guide the gown page opens from its "Size Guide" link.

One table per category, kept here as plain data (not in the database) so the numbers are easy to read, check against a tape measure,
and change in one place. A category with no table has no Size Guide link at all -- the link used to be there on every gown and did
nothing. To give another category a guide, add its entry to SIZE_GUIDES below; the page, the animation and the tests follow it.

Suit, Wedding Gown, Filipiniana, Dresses, Barong, Mother Gown, Guest Gown, Evening Gown, Luxury Gown, Long Gown, Bridesmaid Dresses, Ball Gown Tulle and Kids Gown have one so far. The sizes run S, M, L, XL, XXL like the size buttons on the gown
page (the Mother Gown, Guest Gown, Evening Gown, Luxury Gown, Long Gown, Bridesmaid Dresses and Ball Gown Tulle charts stop at XL, so their tables have four rows). Each measurement is written exactly as the shop's chart prints it (centimetres first, inches after), so nothing is converted or
rounded here. The page shows only the table, and a category's columns are its own (a suit lists chest, shoulder, sleeve and waist; a
wedding gown lists bust, waist and hips).

A guide may also say more than the plain "Size" column: `first_heading` / a row's `label` (Kids Gown's first column is the child's age, "5-6 Years") and
`headings` (the exact column headings, when they are not "<column> (cm / in)"). A row's `code` is still the size on the gown page (S, M, L, XL) -- that is
what the highlight follows."""

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

MOTHER_GUIDE = {
    "title": "Mother Gown size guide",
    "columns": ["Bust", "Waist", "Hips"],
    "rows": [
        {"code": "S", "values": [("84–88 cm", '(33–34.5")'), ("66–70 cm", '(26–27.5")'), ("90–94 cm", '(35.5–37")')]},
        {"code": "M", "values": [("89–93 cm", '(35–36.5")'), ("71–75 cm", '(28–29.5")'), ("95–99 cm", '(37.5–39")')]},
        {"code": "L", "values": [("94–100 cm", '(37–39.5")'), ("76–82 cm", '(30–32")'), ("100–106 cm", '(39.5–41.5")')]},
        {"code": "XL", "values": [("101–108 cm", '(40–42.5")'), ("83–90 cm", '(32.5–35.5")'), ("107–114 cm", '(42–45")')]},
    ],
}

GUEST_GUIDE = {
    "title": "Guest Gown size guide",
    "columns": ["Bust", "Waist", "Hips"],
    "rows": [
        {"code": "S", "values": [("82–86 cm", '(32–34")'), ("64–68 cm", '(25–27")'), ("88–92 cm", '(34.5–36")')]},
        {"code": "M", "values": [("87–91 cm", '(34.5–36")'), ("69–73 cm", '(27–29")'), ("93–97 cm", '(36.5–38")')]},
        {"code": "L", "values": [("92–98 cm", '(36.5–38.5")'), ("74–80 cm", '(29–31.5")'), ("98–104 cm", '(38.5–41")')]},
        {"code": "XL", "values": [("99–106 cm", '(39–42")'), ("81–88 cm", '(32–34.5")'), ("105–112 cm", '(41.5–44")')]},
    ],
}

# The owner's Evening Gown chart prints inches first, then (cm); like every other guide it is shown centimetres first -- the numbers are the owner's, unchanged.
EVENING_GUIDE = {
    "title": "Evening Gown size guide",
    "columns": ["Bust", "Waist", "Hips"],
    "rows": [
        {"code": "S", "values": [("81–86 cm", '(32–34")'), ("61–66 cm", '(24–26")'), ("86–91 cm", '(34–36")')]},
        {"code": "M", "values": [("89–91 cm", '(35–36")'), ("68–72 cm", '(27–28")'), ("94–98 cm", '(37–38")')]},
        {"code": "L", "values": [("94–99 cm", '(37–39")'), ("73–79 cm", '(29–31")'), ("99–107 cm", '(39–42")')]},
        {"code": "XL", "values": [("101–107 cm", '(40–42")'), ("81–87 cm", '(32–34")'), ("109–114 cm", '(43–45")')]},
    ],
}

# The owner gave Luxury Gown the same numbers as Evening Gown (also printed inches first, shown cm first here); it keeps its OWN copy, so editing one never changes the other.
LUXURY_GUIDE = {
    "title": "Luxury Gown size guide",
    "columns": ["Bust", "Waist", "Hips"],
    "rows": [
        {"code": "S", "values": [("81–86 cm", '(32–34")'), ("61–66 cm", '(24–26")'), ("86–91 cm", '(34–36")')]},
        {"code": "M", "values": [("89–91 cm", '(35–36")'), ("68–72 cm", '(27–28")'), ("94–98 cm", '(37–38")')]},
        {"code": "L", "values": [("94–99 cm", '(37–39")'), ("73–79 cm", '(29–31")'), ("99–107 cm", '(39–42")')]},
        {"code": "XL", "values": [("101–107 cm", '(40–42")'), ("81–87 cm", '(32–34")'), ("109–114 cm", '(43–45")')]},
    ],
}

# The owner gave Long Gown the same numbers as Evening Gown and Luxury Gown too; each keeps its OWN copy, so editing one never changes the others.
LONG_GUIDE = {
    "title": "Long Gown size guide",
    "columns": ["Bust", "Waist", "Hips"],
    "rows": [
        {"code": "S", "values": [("81–86 cm", '(32–34")'), ("61–66 cm", '(24–26")'), ("86–91 cm", '(34–36")')]},
        {"code": "M", "values": [("89–91 cm", '(35–36")'), ("68–72 cm", '(27–28")'), ("94–98 cm", '(37–38")')]},
        {"code": "L", "values": [("94–99 cm", '(37–39")'), ("73–79 cm", '(29–31")'), ("99–107 cm", '(39–42")')]},
        {"code": "XL", "values": [("101–107 cm", '(40–42")'), ("81–87 cm", '(32–34")'), ("109–114 cm", '(43–45")')]},
    ],
}

# The owner gave Bridesmaid Dresses and Ball Gown Tulle the same numbers as Evening Gown, Luxury Gown and Long Gown as well; each category keeps its OWN copy.
BRIDESMAID_GUIDE = {
    "title": "Bridesmaid Dresses size guide",
    "columns": ["Bust", "Waist", "Hips"],
    "rows": [
        {"code": "S", "values": [("81–86 cm", '(32–34")'), ("61–66 cm", '(24–26")'), ("86–91 cm", '(34–36")')]},
        {"code": "M", "values": [("89–91 cm", '(35–36")'), ("68–72 cm", '(27–28")'), ("94–98 cm", '(37–38")')]},
        {"code": "L", "values": [("94–99 cm", '(37–39")'), ("73–79 cm", '(29–31")'), ("99–107 cm", '(39–42")')]},
        {"code": "XL", "values": [("101–107 cm", '(40–42")'), ("81–87 cm", '(32–34")'), ("109–114 cm", '(43–45")')]},
    ],
}

BALL_GOWN_TULLE_GUIDE = {
    "title": "Ball Gown Tulle size guide",
    "columns": ["Bust", "Waist", "Hips"],
    "rows": [
        {"code": "S", "values": [("81–86 cm", '(32–34")'), ("61–66 cm", '(24–26")'), ("86–91 cm", '(34–36")')]},
        {"code": "M", "values": [("89–91 cm", '(35–36")'), ("68–72 cm", '(27–28")'), ("94–98 cm", '(37–38")')]},
        {"code": "L", "values": [("94–99 cm", '(37–39")'), ("73–79 cm", '(29–31")'), ("99–107 cm", '(39–42")')]},
        {"code": "XL", "values": [("101–107 cm", '(40–42")'), ("81–87 cm", '(32–34")'), ("109–114 cm", '(43–45")')]},
    ],
}

# Kids Gown: the chart is by AGE, with the shop's size tag beside it. The shop's own sizes are S, M, L and XL; the chart's first row (3-4 years, "XS") is a size the
# shop does not make, so it has the code "XS", which no size button on the gown page ever matches (it is shown, never highlighted).
KIDS_GUIDE = {
    "title": "Kids Gown size guide",
    "first_heading": "Age",
    "columns": ["Size tag", "Chest / Bust", "Waist", "Approximate Length"],
    "headings": ["Size", "Chest / Bust (cm / in)", "Waist (cm / in)", "Approx. Length (Shoulder to Hem)"],
    "rows": [
        {"code": "XS", "label": "3–4 Years", "values": [("Size 3–4", "(XS)"), ("56–58 cm", '(22–23")'), ("53–56 cm", '(21–22")'), ("76–86 cm", '(30–34")')]},
        {"code": "S", "label": "5–6 Years", "values": [("Size 5–6", "(Small)"), ("61–64 cm", '(24–25")'), ("58–61 cm", '(23–24")'), ("91–102 cm", '(36–40")')]},
        {"code": "M", "label": "7–8 Years", "values": [("Size 7–8", "(Medium)"), ("66–69 cm", '(26–27")'), ("64–66 cm", '(25–26")'), ("107–117 cm", '(42–46")')]},
        {"code": "L", "label": "9–10 Years", "values": [("Size 9–10", "(Large)"), ("71–74 cm", '(28–29")'), ("66–69 cm", '(26–27")'), ("122–127 cm", '(48–50")')]},
        {"code": "XL", "label": "11–12 Years", "values": [("Size 11–12", "(XL)"), ("76–81 cm", '(30–32")'), ("71–74 cm", '(28–29")'), ("132–137 cm", '(52–54")')]},
    ],
}

# Keyed by the category's name as the shop writes it (Gown.category), not by its web address.
SIZE_GUIDES = {
    "Suit": SUIT_GUIDE,
    "Wedding Gown": WEDDING_GUIDE,
    "Filipiniana": FILIPINIANA_GUIDE,
    "Dresses": DRESSES_GUIDE,
    "Barong": BARONG_GUIDE,
    "Mother Gown": MOTHER_GUIDE,
    "Guest Gown": GUEST_GUIDE,
    "Evening Gown": EVENING_GUIDE,
    "Luxury Gown": LUXURY_GUIDE,
    "Long Gown": LONG_GUIDE,
    "Bridesmaid Dresses": BRIDESMAID_GUIDE,
    "Ball Gown Tulle": BALL_GOWN_TULLE_GUIDE,
    "Kids Gown": KIDS_GUIDE,
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
        {"code": row["code"], "label": row.get("label", row["code"]), "values": [{"cm": cm, "inch": inch} for cm, inch in row["values"]]}
        for row in guide["rows"]
    ]
    headings = guide.get("headings") or [f"{column} (cm / in)" for column in guide["columns"]]
    return {
        "title": guide["title"], "first_heading": guide.get("first_heading", "Size"), "columns": guide["columns"], "headings": headings, "rows": rows,
    }
