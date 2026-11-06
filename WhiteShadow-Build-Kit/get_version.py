import re
print(re.search(r'^APP, VER = "White Shadow", "([^"]+)"', open("WhiteShadowLauncher.py", encoding="utf-8").read(), re.M).group(1))
