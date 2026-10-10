from appium import webdriver
from appium.options.android import UiAutomator2Options
import time

options = UiAutomator2Options()
options.platform_name = "Android"
options.automation_name = "UiAutomator2"
options.device_name = "10CEBS1Z6L000EQ"
options.app_package = "cn.damai"
options.app_activity = ".launcher.splash.SplashMainActivity"
options.no_reset = True
options.new_command_timeout = 300

print("正在连接 Appium...")

driver = webdriver.Remote(
    "http://127.0.0.1:4723",
    options=options
)

print("连接成功！")
print("当前包名：", driver.current_package)
print("当前 Activity：", driver.current_activity)

time.sleep(3)

print("正在读取大麦页面...")
xml = driver.page_source

print("页面 XML 长度：", len(xml))

for keyword in ["大麦", "首页", "我的", "搜索"]:
    print(f"是否包含「{keyword}」：", keyword in xml)

input("测试完成，按 Enter 结束...")

driver.quit()
print("测试结束")