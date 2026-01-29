"""
Preset tariff templates for common Australian electricity plans.

These templates follow Tesla's tariff_content format and can be used as starting
points for custom tariff configuration by non-Amber users.

Day of Week Mapping (Tesla format):
- 0 = Sunday
- 1 = Monday
- 2 = Tuesday
- 3 = Wednesday
- 4 = Thursday
- 5 = Friday
- 6 = Saturday
"""

from typing import Dict, Any

# Common TOU period definitions
WEEKDAY_PEAK_3PM_9PM = [
    {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 15, "toHour": 21}
]

WEEKDAY_SHOULDER_7AM_3PM = [
    {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 7, "toHour": 15}
]

WEEKDAY_OFFPEAK_9PM_7AM = [
    {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 21, "toHour": 24},
    {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 0, "toHour": 7},
]

WEEKEND_ALL_DAY = [
    {"fromDayOfWeek": 0, "toDayOfWeek": 0, "fromHour": 0, "toHour": 24},  # Sunday
    {"fromDayOfWeek": 6, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24},  # Saturday
]

SOLAR_SOAK_10AM_2PM = [
    {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 10, "toHour": 14}
]


TARIFF_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "globird_tou": {
        "id": "globird_tou",
        "name": "Globird Time of Use",
        "utility": "Globird Energy",
        "description": "Standard Globird TOU tariff with peak, shoulder, and off-peak rates",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": {
                    "PEAK": WEEKDAY_PEAK_3PM_9PM,
                    "SHOULDER": WEEKDAY_SHOULDER_7AM_3PM,
                    "OFF_PEAK": [
                        *WEEKDAY_OFFPEAK_9PM_7AM,
                        *WEEKEND_ALL_DAY,
                    ],
                },
            }
        },
        "energy_charges": {
            "All Year": {
                "PEAK": 0.42,       # 42c/kWh
                "SHOULDER": 0.25,   # 25c/kWh
                "OFF_PEAK": 0.14,   # 14c/kWh
            }
        },
        "sell_tariff": {
            "energy_charges": {
                "All Year": {
                    "ALL": 0.05,    # 5c/kWh flat FiT
                }
            }
        },
    },

    "agl_solar_savers": {
        "id": "agl_solar_savers",
        "name": "AGL Solar Savers",
        "utility": "AGL",
        "description": "AGL Solar Savers with free 10am-2pm solar soak period",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": {
                    "SUPER_OFF_PEAK": SOLAR_SOAK_10AM_2PM,
                    "PEAK": WEEKDAY_PEAK_3PM_9PM,
                    "OFF_PEAK": [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 21, "toHour": 24},
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 0, "toHour": 7},
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 7, "toHour": 10},
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 14, "toHour": 15},
                        *WEEKEND_ALL_DAY,
                    ],
                },
            }
        },
        "energy_charges": {
            "All Year": {
                "SUPER_OFF_PEAK": 0.0,  # Free solar soak period
                "PEAK": 0.48,           # 48c/kWh
                "OFF_PEAK": 0.20,       # 20c/kWh
            }
        },
        "sell_tariff": {
            "energy_charges": {
                "All Year": {
                    "ALL": 0.05,    # 5c/kWh flat FiT
                }
            }
        },
    },

    "origin_solar_boost": {
        "id": "origin_solar_boost",
        "name": "Origin Solar Boost",
        "utility": "Origin Energy",
        "description": "Origin Solar Boost with higher FiT and TOU pricing",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": {
                    "PEAK": WEEKDAY_PEAK_3PM_9PM,
                    "SHOULDER": WEEKDAY_SHOULDER_7AM_3PM,
                    "OFF_PEAK": [
                        *WEEKDAY_OFFPEAK_9PM_7AM,
                        *WEEKEND_ALL_DAY,
                    ],
                },
            }
        },
        "energy_charges": {
            "All Year": {
                "PEAK": 0.45,       # 45c/kWh
                "SHOULDER": 0.28,   # 28c/kWh
                "OFF_PEAK": 0.16,   # 16c/kWh
            }
        },
        "sell_tariff": {
            "energy_charges": {
                "All Year": {
                    "ALL": 0.07,    # 7c/kWh flat FiT
                }
            }
        },
    },

    "energyaustralia_tou": {
        "id": "energyaustralia_tou",
        "name": "EnergyAustralia Time of Use",
        "utility": "EnergyAustralia",
        "description": "Standard EnergyAustralia TOU tariff",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": {
                    "PEAK": [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 14, "toHour": 20}
                    ],
                    "SHOULDER": [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 7, "toHour": 14},
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 20, "toHour": 22},
                        {"fromDayOfWeek": 6, "toDayOfWeek": 6, "fromHour": 7, "toHour": 22},
                        {"fromDayOfWeek": 0, "toDayOfWeek": 0, "fromHour": 7, "toHour": 22},
                    ],
                    "OFF_PEAK": [
                        {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 22, "toHour": 24},
                        {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 7},
                    ],
                },
            }
        },
        "energy_charges": {
            "All Year": {
                "PEAK": 0.52,       # 52c/kWh
                "SHOULDER": 0.28,   # 28c/kWh
                "OFF_PEAK": 0.18,   # 18c/kWh
            }
        },
        "sell_tariff": {
            "energy_charges": {
                "All Year": {
                    "ALL": 0.05,    # 5c/kWh flat FiT
                }
            }
        },
    },

    "flat_rate": {
        "id": "flat_rate",
        "name": "Flat Rate Tariff",
        "utility": "Generic",
        "description": "Simple flat rate tariff - same price all day",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": {
                    "ALL": [
                        {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 24}
                    ],
                },
            }
        },
        "energy_charges": {
            "All Year": {
                "ALL": 0.30,        # 30c/kWh flat rate
            }
        },
        "sell_tariff": {
            "energy_charges": {
                "All Year": {
                    "ALL": 0.05,    # 5c/kWh flat FiT
                }
            }
        },
    },

    "simple_tou": {
        "id": "simple_tou",
        "name": "Simple Time of Use",
        "utility": "Generic",
        "description": "Basic TOU with peak (3-9pm weekdays) and off-peak",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": {
                    "PEAK": WEEKDAY_PEAK_3PM_9PM,
                    "OFF_PEAK": [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 0, "toHour": 15},
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 21, "toHour": 24},
                        *WEEKEND_ALL_DAY,
                    ],
                },
            }
        },
        "energy_charges": {
            "All Year": {
                "PEAK": 0.45,       # 45c/kWh
                "OFF_PEAK": 0.15,   # 15c/kWh
            }
        },
        "sell_tariff": {
            "energy_charges": {
                "All Year": {
                    "ALL": 0.05,    # 5c/kWh flat FiT
                }
            }
        },
    },

    "solar_sponge": {
        "id": "solar_sponge",
        "name": "Solar Sponge (Free Daytime)",
        "utility": "Generic",
        "description": "Tariff with free daytime period (9am-3pm) to encourage solar usage",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": {
                    "SUPER_OFF_PEAK": [
                        {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 9, "toHour": 15}
                    ],
                    "PEAK": WEEKDAY_PEAK_3PM_9PM,
                    "OFF_PEAK": [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 0, "toHour": 9},
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 21, "toHour": 24},
                        {"fromDayOfWeek": 0, "toDayOfWeek": 0, "fromHour": 0, "toHour": 9},
                        {"fromDayOfWeek": 0, "toDayOfWeek": 0, "fromHour": 15, "toHour": 24},
                        {"fromDayOfWeek": 6, "toDayOfWeek": 6, "fromHour": 0, "toHour": 9},
                        {"fromDayOfWeek": 6, "toDayOfWeek": 6, "fromHour": 15, "toHour": 24},
                    ],
                },
            }
        },
        "energy_charges": {
            "All Year": {
                "SUPER_OFF_PEAK": 0.0,  # Free solar sponge period
                "PEAK": 0.50,           # 50c/kWh
                "OFF_PEAK": 0.20,       # 20c/kWh
            }
        },
        "sell_tariff": {
            "energy_charges": {
                "All Year": {
                    "ALL": 0.0,     # No FiT on solar sponge plans
                }
            }
        },
    },

    "ev_friendly": {
        "id": "ev_friendly",
        "name": "EV Friendly (Cheap Night)",
        "utility": "Generic",
        "description": "Tariff with super cheap overnight rates for EV charging (12am-6am)",
        "seasons": {
            "All Year": {
                "fromMonth": 1,
                "toMonth": 12,
                "tou_periods": {
                    "SUPER_OFF_PEAK": [
                        {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "toHour": 6}
                    ],
                    "PEAK": WEEKDAY_PEAK_3PM_9PM,
                    "SHOULDER": [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 6, "toHour": 15},
                    ],
                    "OFF_PEAK": [
                        {"fromDayOfWeek": 1, "toDayOfWeek": 5, "fromHour": 21, "toHour": 24},
                        *WEEKEND_ALL_DAY,
                    ],
                },
            }
        },
        "energy_charges": {
            "All Year": {
                "SUPER_OFF_PEAK": 0.08, # 8c/kWh overnight
                "PEAK": 0.45,           # 45c/kWh
                "SHOULDER": 0.28,       # 28c/kWh
                "OFF_PEAK": 0.18,       # 18c/kWh
            }
        },
        "sell_tariff": {
            "energy_charges": {
                "All Year": {
                    "ALL": 0.05,    # 5c/kWh flat FiT
                }
            }
        },
    },
}


def get_template(template_id: str) -> Dict[str, Any] | None:
    """Get a tariff template by ID."""
    return TARIFF_TEMPLATES.get(template_id)


def get_all_templates() -> Dict[str, Dict[str, Any]]:
    """Get all available tariff templates."""
    return TARIFF_TEMPLATES
