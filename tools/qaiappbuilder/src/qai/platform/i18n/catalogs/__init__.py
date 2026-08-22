from . import en, zh_CN, zh_TW

CATALOGS: dict[str, dict[str, str]] = {
    "en": en.MESSAGES,
    "zh-CN": zh_CN.MESSAGES,
    "zh-TW": zh_TW.MESSAGES,
}
