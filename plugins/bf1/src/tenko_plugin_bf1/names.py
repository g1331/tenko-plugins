"""地图、模式、精英兵的中文名，和 bf-manager 网页的叫法一致。不依赖插件里的其他模块。"""

from __future__ import annotations

# apps/web/lib/bf1/catalog.ts
MAPS = {
    "MP_MountainFort": "格拉巴山",
    "MP_Forest": "阿尔贡森林",
    "MP_ItalianCoast": "帝国边境",
    "MP_Chateau": "流血宴厅",
    "MP_Scar": "圣康坦的伤痕",
    "MP_Desert": "西奈沙漠",
    "MP_Amiens": "亚眠",
    "MP_Suez": "苏伊士",
    "MP_FaoFortress": "法欧堡",
    "MP_Giant": "庞然暗影",
    "MP_Fields": "苏瓦松",
    "MP_Graveyard": "决裂",
    "MP_Underworld": "法乌克斯要塞",
    "MP_Verdun": "凡尔登高地",
    "MP_Trench": "尼维尔之夜",
    "MP_ShovelTown": "攻占托尔",
    "MP_Bridge": "勃鲁希洛夫关口",
    "MP_Islands": "阿尔比恩",
    "MP_Ravines": "武普库夫山口",
    "MP_Valley": "加利西亚",
    "MP_Tsaritsyn": "察里津",
    "MP_Volga": "窝瓦河",
    "MP_Beachhead": "海丽丝岬",
    "MP_Harbor": "泽布吕赫",
    "MP_Naval": "黑尔戈兰湾",
    "MP_Ridge": "阿奇巴巴",
    "MP_Offensive": "索姆河",
    "MP_Hell": "帕斯尚尔",
    "MP_River": "卡波雷托",
    "MP_Alps": "剃刀边缘",
    "MP_Blitz": "伦敦的呼唤：夜袭",
    "MP_London": "伦敦的呼唤：灾祸",
}

# 小贴士标签里的模式代号
MODES = {
    "Conquest": "征服",
    "Breakthrough": "闪击行动",
    "Domination": "抢攻",
    "Possession": "战争信鸽",
    "Rush": "突袭",
    "TeamDeathMatch": "团队死斗",
}

# 生涯战绩里的模式（apps/web/components/bf1/player-detail/CareerHonors.tsx）
CAREER_MODES = {
    "conquest": "征服",
    "operations": "行动模式",
    "rush": "突袭",
    "frontlines": "前线",
    "domination": "抢攻",
    "war_pigeons": "战争信鸽",
    "team_deathmatch": "团队死斗",
    "air_assault": "空中突击",
}

ELITE_KITS = {
    "sentry": "哨兵",
    "flamethrower": "喷火兵",
    "trench_raider": "战壕奇兵",
    "tank_hunter": "坦克猎手",
    "infiltrator": "入侵者",
}
