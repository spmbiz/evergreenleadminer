#!/usr/bin/env python3
"""Additive Overture category coverage for the expanded GWS family set.

This module deliberately reuses the existing gws_source_overture transport,
owned-site screening, chain screening, postcode validation, dedupe and persistence
logic. It only extends category -> discovery-type normalization so newly configured
families can actually materialize candidates.

It never emits strict HIGH and does not change canonical verification semantics.
"""
from __future__ import annotations

import re
from typing import Any

import gws_source_overture as base


_legacy_category_type = base.category_type


def _category_tokens(value: Any) -> str:
    """Normalize one structured category while preserving token boundaries.

    Overture categories are underscore-delimited. Matching by raw substring is
    unsafe: e.g. ``pub`` used to match ``public_plaza`` and materialized public
    squares as pubs. A padded underscore string lets short needles such as
    ``pub``, ``bar`` or ``gym`` match only real category tokens while still
    allowing phrases such as ``cocktail_bar`` or ``real_estate_agency``.
    """
    normalized = base.norm(value).replace(" ", "_")
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return f"_{normalized}_" if normalized else "_"


def _matches_category(basic: Any, primary: Any, needle: str) -> bool:
    token = re.sub(r"_+", "_", base.norm(needle).replace(" ", "_")).strip("_")
    if not token:
        return False
    marker = f"_{token}_"
    return marker in _category_tokens(primary) or marker in _category_tokens(basic)


def category_type(basic: Any, primary: Any) -> str:
    # Preserve every mapping that already worked before this extension.
    legacy = _legacy_category_type(basic, primary)
    if legacy:
        return legacy

    # Outputs intentionally contain the same vocabulary used by
    # config/gws_fleet.json keywords so gws_fleet_worker.target_row() can select
    # them without changing verification or ranking semantics.
    rules: list[tuple[tuple[str, ...], str]] = [
        # restaurants_cafes
        (("restaurant",), "Restaurant"),
        (("coffee_shop", "coffeehouse", "cafe"), "Cafe"),
        (("tea_room", "tea_house"), "Tea room"),
        (("fast_food",), "Fast food"),
        (("pizzeria", "pizza_restaurant"), "Pizzeria"),
        (("brasserie",), "Brasserie"),
        (("food_delivery_service",), "Food delivery service"),

        # bars_nightlife. Short tokens are intentionally boundary-matched so
        # ``pub`` never matches ``public_plaza`` / ``public_utility_company``.
        # ``gastropub`` is restored explicitly because it is a legitimate bar
        # category that old substring matching used to include accidentally.
        (("cocktail_bar",), "Cocktail bar"),
        (("nightclub", "night_club"), "Nightclub"),
        (("tavern",), "Tavern"),
        (("gastropub",), "Pub"),
        (("pub",), "Pub"),
        (("bar",), "Bar"),

        # ethnic_grocery_specialty_food
        (("supermarket",), "Supermarket"),
        (("grocery", "grocery_store", "food_store", "food_market"), "Grocery"),
        (("delicatessen", "deli"), "Delicatessen"),
        (("spice_shop", "spice_store", "spices"), "Spices"),
        (("organic_food", "health_food_store"), "Organic food"),
        (("specialty_food", "speciality_food"), "Specialty food"),

        # book_news_gifts
        (("bookstore", "book_store"), "Bookstore"),
        (("newsagent", "newsstand", "newspaper_store"), "Newsagent"),
        (("stationery", "stationery_store"), "Stationery"),
        (("souvenir", "souvenir_shop"), "Souvenir"),
        (("gift_shop", "gift_store"), "Gift shop"),

        # fashion_shoes_accessories
        (("shoe_store", "footwear", "shoes"), "Shoes"),
        (("leather_goods",), "Leather goods"),
        (("fashion_accessories", "accessories_store"), "Accessories"),
        (("clothing_store", "clothes", "apparel"), "Clothing fashion boutique"),
        (("fashion", "boutique"), "Fashion boutique"),

        # home_furniture_hardware
        (("furniture_store", "furniture"), "Furniture"),
        (("home_decor", "home_goods_store", "housewares"), "Home decor"),
        (("lighting_store", "lighting"), "Lighting"),
        (("kitchen_supply", "kitchen_store"), "Kitchen"),
        (("bathroom_supply", "bathroom_store"), "Bathroom"),
        (("hardware_store", "hardware"), "Hardware"),
        (("interior_decoration", "interior_decorator"), "Interior decoration"),

        # construction_trades
        (("general_contractor", "construction_company", "construction_service"), "Construction"),
        (("carpenter", "carpentry"), "Carpenter"),
        (("house_painter", "painting_contractor", "painter"), "Painter"),
        (("roofer", "roofing_contractor"), "Roofer"),
        (("tiler", "tile_contractor"), "Tiler"),
        (("glazier", "glass_service", "glazing"), "Glazing"),
        (("masonry", "mason"), "Masonry"),

        # cleaning_moving_pest
        (("window_cleaning", "window_cleaner"), "Window cleaning"),
        (("cleaning_service", "cleaner"), "Cleaning"),
        (("pest_control", "exterminator"), "Pest control"),
        (("moving_company", "moving_service", "mover", "movers", "removal_service"), "Moving"),
        (("storage_facility", "self_storage"), "Storage"),

        # health_clinics
        (("dentist", "dental_clinic"), "Dentist dental"),
        (("physiotherapist", "physical_therapy", "physiotherapy"), "Physiotherapy physiotherapist"),
        (("chiropractor",), "Chiropractor"),
        (("psychologist", "psychotherapy"), "Psychologist"),
        (("speech_therapy", "speech_therapist", "speech_pathology", "speech_pathologist"), "Speech therapy"),
        (("medical_clinic", "health_clinic", "clinic"), "Clinic"),

        # fitness_sports_dance
        (("yoga_studio", "yoga"), "Yoga"),
        (("pilates_studio", "pilates"), "Pilates"),
        (("martial_arts",), "Martial arts"),
        (("dance_school", "dance_studio"), "Dance school"),
        (("sports_club",), "Sports club"),
        (("fitness_center", "fitness_centre", "gym", "gymnastics_center"), "Gym fitness"),

        # education_children
        (("driving_school",), "Driving school"),
        (("tutoring", "tutor"), "Tutoring"),
        (("language_school",), "Language school"),
        (("childcare", "child_care"), "Childcare"),
        (("daycare", "day_care"), "Daycare"),
        (("nursery_school", "nursery"), "Nursery"),
        (("music_school",), "Music school"),

        # creative_events (photographer legacy mapping intentionally remains
        # handled above; these add categories not previously materialized)
        (("videographer", "video_production_service"), "Videographer"),
        (("wedding_planner",), "Wedding planner"),
        (("event_planner", "event_management"), "Event planner"),
        (("art_gallery",), "Art gallery"),
        (("picture_framing", "frame_shop", "framing"), "Framing"),

        # vehicle_extended
        (("car_wash",), "Car wash"),
        (("auto_detail", "car_detail", "auto_detailing"), "Car detailing"),
        (("motorcycle_shop", "motorcycle_dealer", "motorcycle_repair"), "Motorcycle"),
        (("bicycle_repair", "bike_repair"), "Bike repair"),
        (("bicycle_shop", "bike_shop", "cycle_shop"), "Bicycle cycle shop"),
        (("vehicle_shipping",), "Vehicle shipping"),

        # professional_local_services
        (("accountant", "accounting_firm", "accounting_service"), "Accountant accounting"),
        (("insurance_broker", "insurance_agency", "insurance_agent"), "Insurance broker"),
        (("real_estate_agency", "estate_agent", "real_estate_agent"), "Real estate agency"),
        (("architect", "architecture_firm", "architectural_designer"), "Architect"),
        (("surveyor", "land_surveyor"), "Surveyor"),
        (("translation_service", "translator"), "Translation"),
        (("notary_public",), "Notary"),
        (("public_relations",), "Public relations"),
    ]
    for needles, typ in rules:
        if any(_matches_category(basic, primary, needle) for needle in needles):
            return typ
    return ""


base.category_type = category_type


if __name__ == "__main__":
    raise SystemExit(base.main())
