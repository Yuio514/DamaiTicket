from appium import webdriver
from appium.options.android import UiAutomator2Options

options = UiAutomator2Options()

options.platform_name = "Android"
options.automation_name = "UiAutomator2"
options.device_name = "Android"

options.app_package = "com.android.bbkcalculator"
options.app_activity = ".Calculator"

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

input("手机已经打开计算器。按 Enter 结束测试...")

driver.quit()

print("测试结束")