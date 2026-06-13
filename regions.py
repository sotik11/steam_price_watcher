"""Steam country + currency tables.

Steam Market endpoints take a `country` (ISO 3166-1 alpha-2) and a
`currency` (Steam's own integer code) on every request. The Settings
tab shows both as dropdowns; this module is the single source of
truth for the values that go in them, plus the country→currency
mapping that auto-syncs the two pickers.

The mapping isn't a hard binding — the user can pick a country and
then change the currency to whatever they want. We only nudge the
currency dropdown when a country is selected; we never *force* a
currency on save. That covers the common "I'm in PL but I want my
Market in EUR" workflow without locking it down.

Sources:
  * Steam currencies: Valve's pricing docs
    (`partner.steamgames.com/doc/store/pricing/currencies`). Codes 33
    and 36 are reserved and currently unused — left out here.
  * Country list: ~85 countries covering everywhere Steam Market
    actually settles transactions in a distinct currency, plus the
    full Eurozone for completeness (every EUR country maps to currency
    code 3).
"""

# Steam internal currency code → ISO 4217-ish 3-letter code used in
# the Settings dropdown display ("UAH (18)"). The integer code is what
# goes on the wire; the 3-letter symbol is purely cosmetic for users
# who recognise "USD" faster than "1".
STEAM_CURRENCIES: dict[int, str] = {
    1:  "USD",  2:  "GBP",  3:  "EUR",  4:  "CHF",  5:  "RUB",
    6:  "PLN",  7:  "BRL",  8:  "JPY",  9:  "NOK",  10: "IDR",
    11: "MYR",  12: "PHP",  13: "SGD",  14: "THB",  15: "VND",
    16: "KRW",  17: "TRY",  18: "UAH",  19: "MXN",  20: "CAD",
    21: "AUD",  22: "NZD",  23: "CNY",  24: "INR",  25: "CLP",
    26: "PEN",  27: "COP",  28: "ZAR",  29: "HKD",  30: "TWD",
    31: "SAR",  32: "AED",  34: "ARS",  35: "ILS",  37: "KZT",
    38: "KWD",  39: "QAR",  40: "CRC",  41: "UYU",
}

# Currency symbol used in the floating Steam widget's placeholder
# "0.00 X" string and anywhere else we need a compact glyph (alerts,
# UI tooltips). For currencies without a single dedicated glyph
# (CHF, MYR, …) we fall back to the 3-letter code — better an honest
# "CHF" than a generic "$" that lies about which currency it is.
CURRENCY_SYMBOLS: dict[int, str] = {
    1:  "$",     2:  "£",    3:  "€",    4:  "CHF",  5:  "₽",
    6:  "zł",    7:  "R$",   8:  "¥",    9:  "kr",   10: "Rp",
    11: "RM",    12: "₱",    13: "S$",   14: "฿",    15: "₫",
    16: "₩",     17: "₺",    18: "₴",    19: "Mex$", 20: "CDN$",
    21: "A$",    22: "NZ$",  23: "¥",    24: "₹",    25: "CLP$",
    26: "S/.",   27: "COL$", 28: "R",    29: "HK$",  30: "NT$",
    31: "SR",    32: "AED",  34: "ARS$", 35: "₪",    37: "₸",
    38: "KD",    39: "QR",   40: "₡",    41: "$U",
}


def currency_symbol(code: int, fallback: str = "$") -> str:
    """Compact display glyph for a Steam currency code.

    Used by the floating user widget to render the placeholder
    "0.00 X" balance string. The Steam-served wallet balance comes
    with its own symbol embedded — we only need this lookup when
    showing a synthesised placeholder.
    """
    return CURRENCY_SYMBOLS.get(code, fallback)


# (ISO2 country code, English display name, Steam currency code).
# Sorted alphabetically by display name to keep the dropdown navigable.
# All eurozone members share currency 3 (EUR); a handful of non-EU
# countries (ME, SM, VA, etc.) also pin to EUR by Steam's convention.
STEAM_COUNTRIES: list[tuple[str, str, int]] = [
    ("AE", "United Arab Emirates", 32),
    ("AR", "Argentina",              34),
    ("AT", "Austria",                3),
    ("AU", "Australia",              21),
    ("BE", "Belgium",                3),
    ("BG", "Bulgaria",               3),
    ("BR", "Brazil",                 7),
    ("BY", "Belarus",                5),
    ("CA", "Canada",                 20),
    ("CH", "Switzerland",            4),
    ("CL", "Chile",                  25),
    ("CN", "China",                  23),
    ("CO", "Colombia",               27),
    ("CR", "Costa Rica",             40),
    ("CY", "Cyprus",                 3),
    ("CZ", "Czechia",                3),
    ("DE", "Germany",                3),
    ("DK", "Denmark",                3),
    ("EE", "Estonia",                3),
    ("ES", "Spain",                  3),
    ("FI", "Finland",                3),
    ("FR", "France",                 3),
    ("GB", "United Kingdom",         2),
    ("GR", "Greece",                 3),
    ("HK", "Hong Kong",              29),
    ("HR", "Croatia",                3),
    ("HU", "Hungary",                3),
    ("ID", "Indonesia",              10),
    ("IE", "Ireland",                3),
    ("IL", "Israel",                 35),
    ("IN", "India",                  24),
    ("IS", "Iceland",                3),
    ("IT", "Italy",                  3),
    ("JP", "Japan",                  8),
    ("KR", "South Korea",            16),
    ("KW", "Kuwait",                 38),
    ("KZ", "Kazakhstan",             37),
    ("LT", "Lithuania",              3),
    ("LU", "Luxembourg",             3),
    ("LV", "Latvia",                 3),
    ("MT", "Malta",                  3),
    ("MX", "Mexico",                 19),
    ("MY", "Malaysia",               11),
    ("NL", "Netherlands",            3),
    ("NO", "Norway",                 9),
    ("NZ", "New Zealand",            22),
    ("PE", "Peru",                   26),
    ("PH", "Philippines",            12),
    ("PL", "Poland",                 6),
    ("PT", "Portugal",               3),
    ("QA", "Qatar",                  39),
    ("RO", "Romania",                3),
    ("RU", "Russia",                 5),
    ("SA", "Saudi Arabia",           31),
    ("SE", "Sweden",                 3),
    ("SG", "Singapore",              13),
    ("SI", "Slovenia",               3),
    ("SK", "Slovakia",               3),
    ("TH", "Thailand",               14),
    ("TR", "Turkey",                 17),
    ("TW", "Taiwan",                 30),
    ("UA", "Ukraine",                18),
    ("US", "United States",          1),
    ("UY", "Uruguay",                41),
    ("VN", "Vietnam",                15),
    ("ZA", "South Africa",           28),
]


def currency_label(code: int) -> str:
    """Format a currency code for the Combobox: 'UAH (18)' / 'USD (1)'.

    Falls back to a bare "(<code>)" for codes we don't have a name
    for — keeps the dropdown usable even if Steam adds a currency we
    haven't catalogued yet.
    """
    sym = STEAM_CURRENCIES.get(code, "")
    return f"{sym} ({code})" if sym else f"({code})"


def country_label(iso: str, name: str) -> str:
    """Format a country entry for the Combobox: 'Ukraine (UA)'."""
    return f"{name} ({iso})"
