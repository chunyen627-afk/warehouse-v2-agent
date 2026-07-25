# -*- coding: utf-8 -*-
"""
make_en_quips.py — 把 association_meta.json 的搭售俏皮話/情境標籤英文化。
只改「訪客看得到的文字」：scenarios[].label、pair_quips[]、scenario_quips[]。
不動 key、sku 清單、emoji。原檔備份成 .zh.bak（只備一次）。
用法：python make_en_quips.py
"""
import json, shutil
from pathlib import Path

F = Path(__file__).parent / "warehouse_data" / "master" / "association_meta.json"

SCENARIO_LABEL_EN = {
    "home_fitness": "Home Fitness",
    "gadget":       "Gadget Unboxing",
    "summer_out":   "Summer Outing",
    "newborn":      "New Parents",
    "coffee":       "Home Coffee Brewing",
    "running":      "Running",
    "daily_buy":    "Everyday Shopping",
    "camping":      "Camping & Cookout",
    "cleaning":     "Spring Cleaning",
    "winter":       "Winter Warmth",
    "home_office":  "Home Office",
    "kitchen_cook": "Kitchen Cooking",
    "basic_wear":   "Basics Wardrobe",
}

PAIR_QUIPS_EN = {
    # 經典購物籃案例：尿布 + 啤酒
    "d06|f06": [
        "Dad on diaper duty needs a cold one (yes, the textbook case)",
        "Baby finally asleep - time for a well-earned beer",
        "The most famous pair in retail analytics",
        "Sounds odd, but the data swears by it",
        "Night shift supplies: one for baby, one for dad",
    ],
    "e01|e03": [
        "Earphones die, cable saves the day",
        "Buy the earphones, buy the cable - simple maths",
        "A charging cable is the vitamin of every gadget",
        "Two-day battery means daily cable hunting",
        "No cable, no music. Enough said",
    ],
    "e01|e02": [
        "Music all day needs a battery that lasts",
        "Earphones plus power bank: the commuter combo",
        "Nothing kills a playlist like 1% battery",
        "Both wireless, both hungry for charge",
        "The out-and-about survival kit",
    ],
    "a06|f02": [
        "A machine with no beans just makes hot water",
        "Beans are the fuel; the machine is only the engine",
        "Buy the machine, then buy beans forever",
        "The machine lasts years, the beans last a fortnight",
        "No beans, no aroma - the maths is simple",
    ],
    "a06|d10": [
        "Filters are the consumable nobody remembers until it's too late",
        "Cheap to buy, painful to run out of",
        "One filter per cup, they vanish fast",
        "The machine is the star; filters are the crew",
        "Run out of filters and the machine is just decor",
    ],
    "a08|s06": [
        "Tent for sleeping, cookware for eating - camping in a nutshell",
        "Nobody drives to the mountains to eat cold bread",
        "Shelter and supper, bought in the same trip",
        "The two heaviest things in every car boot",
        "Set up camp, then start cooking",
    ],
    "d08|s06": [
        "The mountain has mosquitoes. Lots of them",
        "Forget repellent and you'll remember all night",
        "Tent keeps the rain out, repellent keeps the bites out",
        "Cheapest insurance in the whole camping kit",
        "One spray, a night of peace",
    ],
    "s01|s03": [
        "Mat for the floor work, ring for the burn",
        "The living-room gym starter pack",
        "Both fold away, both get used at 11pm",
        "Stretch first, squeeze second",
        "Small kit, surprisingly sore the next day",
    ],
    "c06|s08": [
        "Shoes for the road, shirt for the sweat",
        "Cotton on a 10K run is a rookie mistake",
        "Feet dry, back dry - that's the whole idea",
        "The runner's uniform, bought together",
        "New shoes deserve a proper shirt",
    ],
    "f07|s08": [
        "Run first, rehydrate immediately after",
        "Sweat takes salts with it - put them back",
        "The finish-line reward that's actually useful",
        "Shoes get you there, electrolytes get you home",
        "Every runner's boot has one rolling around",
    ],
    "c02|c03": [
        "Cold feet ruin a warm coat",
        "Layer the body, don't forget the toes",
        "Winter's two most underrated purchases",
        "The jacket gets the credit, the socks do the work",
        "Bought on the same cold morning",
    ],
    "c03|f09": [
        "Warm coat outside, warm cocoa inside",
        "Winter's full comfort package",
        "Come in from the cold, put the kettle on",
        "One warms the body, one warms the mood",
        "The classic cold-snap basket",
    ],
    "d01|d05": [
        "Wash the clothes, bin the rest - cleaning day staples",
        "Both bought by the armful, both run out quietly",
        "The unglamorous heroes of the household",
        "Nobody buys just one of these",
        "Restock day essentials",
    ],
    "d01|d09": [
        "Detergent for the clothes, gloves for your hands",
        "Strong cleaner deserves proper protection",
        "The pair that survives every deep clean",
        "Hands say thank you later",
        "Cleaning kit, complete",
    ],
    "d06|d07": [
        "Diapers and wipes: never buy one without the other",
        "The two things you always need at 3am",
        "Bought in bulk, gone in a week",
        "New-parent maths: always buy more",
        "The nappy bag's inseparable duo",
    ],
}

SCENARIO_QUIPS_EN = {
    "coffee": [
        "Serious about home brewing? Missing one piece ruins it",
        "Building a home cafe - these are the basics",
        "Coffee people buy these as a set",
        "One missing item and the morning ritual falls apart",
        "The home barista's shopping list",
        "Buy them together, brew a proper cup",
    ],
    "camping": [
        "All the things you carry up the mountain",
        "Pack these and you won't be borrowing from neighbours",
        "The gear that separates fun trips from long nights",
        "Forget one and you'll regret it after dark",
        "One trip, one full kit",
        "Camping is fun - as long as nothing's missing",
    ],
    "home_fitness": [
        "The living room becomes the gym",
        "No commute, no queue, no excuses",
        "Small kit, real sweat",
        "Everything folds under the sofa",
        "The at-home training starter set",
    ],
    "newborn": [
        "New-parent survival supplies",
        "Bought by the box, gone in days",
        "The 3am essentials",
        "Stock up now, thank yourself later",
        "Everything the nappy bag needs",
    ],
    "cleaning": [
        "Deep-clean day, full arsenal",
        "One pass through the house needs all of these",
        "The weekend reset kit",
        "Cleaning is easier with the right supplies",
        "Buy together, clean once",
    ],
    "gadget": [
        "Unboxing day essentials",
        "The accessories that make the gadget usable",
        "New device, new cables, new case",
        "Tech people buy these in one go",
        "Protect it, power it, enjoy it",
    ],
    "running": [
        "Road-running kit, head to toe",
        "Shoes, shirt, hydration - the holy trinity",
        "Everything you need before the 6am alarm",
        "Runners rarely buy just one of these",
        "Train properly, recover properly",
    ],
    "winter": [
        "Cold-snap shopping list",
        "Layer up, warm up",
        "The gear that makes winter bearable",
        "Bought the week the temperature drops",
        "Warm outside, warm inside",
    ],
    "home_office": [
        "Working from home, properly equipped",
        "Desk setup plus the snacks that survive deadlines",
        "The remote-work starter pack",
        "Comfortable desk, productive day",
        "Everything within arm's reach of the keyboard",
    ],
    "kitchen_cook": [
        "Cooking at home needs the right tools",
        "Pan, containers, and something to drink",
        "The kitchen basics that get used daily",
        "Cook once, store the rest",
        "Everything for a proper home-cooked meal",
    ],
    "summer_out": [
        "Sun's out - here's the survival kit",
        "Hat, repellent, and something cold",
        "Everything for a day outdoors",
        "Beat the heat, dodge the bites",
        "Summer outings need supplies",
    ],
    "daily_buy": [
        "The everyday restock run",
        "Nothing exciting, everything necessary",
        "The basket that repeats every fortnight",
        "Household staples, bought on autopilot",
        "Run out of these and you notice immediately",
    ],
    "basic_wear": [
        "Wardrobe basics, always in rotation",
        "Nothing flashy, everything worn",
        "The pieces that go with anything",
        "Restocked every season",
        "Simple, reliable, always needed",
    ],
}


def main():
    d = json.load(open(F, encoding="utf-8"))
    bak = F.with_suffix(".json.zh.bak")
    if not bak.exists():
        shutil.copy(F, bak); print(f"[bak] {bak.name}")

    n_lab = n_pair = n_scen = 0
    for k, v in d.get("scenarios", {}).items():
        if k in SCENARIO_LABEL_EN:
            v["label_zh"] = v.get("label")
            v["label"] = SCENARIO_LABEL_EN[k]; n_lab += 1
    for k in list(d.get("pair_quips", {})):
        if k in PAIR_QUIPS_EN:
            d["pair_quips"][k] = PAIR_QUIPS_EN[k]; n_pair += 1
    for k in list(d.get("scenario_quips", {})):
        if k in SCENARIO_QUIPS_EN:
            d["scenario_quips"][k] = SCENARIO_QUIPS_EN[k]; n_scen += 1

    json.dump(d, open(F, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[done] scenarios label {n_lab}, pair_quips {n_pair}, scenario_quips {n_scen}")
    # 殘留中文檢查
    txt = json.dumps(d, ensure_ascii=False)
    cjk = sum(1 for c in txt if "一" <= c <= "鿿")
    print(f"[check] 剩餘中文字元 {cjk}（label_zh 備份欄位會留中文，正常）")


if __name__ == "__main__":
    main()
