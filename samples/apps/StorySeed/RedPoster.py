from selenium import webdriver as WDRV
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait as WREVW
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
import time
import json
import os

DEBUG = False

LOGIN_URL = "https://creator.xiaohongshu.com/login"
RED_URL = "https://creator.xiaohongshu.com"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?from=menu"

class RedPublisher:
    def __init__(self, path=os.path.dirname(__file__)):
        if DEBUG:
            print("Class RedPublisher is init ...")
        self.drv = WDRV.Chrome()
        self.wait = WREVW(self.drv, 10)
        self.f_cookies = os.path.join(path, "red_cookies.json")

    def _load_cks(self):
        if DEBUG:
            print("Try to load cookies from json:", self.f_cookies)
        if os.path.exists(self.f_cookies):
            try:
                with open(self.f_cookies, 'r') as f:
                    cks = json.load(f)
                    self.drv.get(RED_URL)
                    for ck in cks:
                        self.drv.add_cookie(ck)
            except:
                pass

    def _store_cks(self):
        if DEBUG:
            print("Save cookies to json")
        cks = self.drv.get_cookies()
        with open(self.f_cookies, 'w') as f:
            json.dump(cks, f)

    def post(self, title, content, images):
        self._load_cks()
        self.drv.refresh()
        self.drv.get(PUBLISH_URL)
        time.sleep(5)
        if self.drv.current_url != PUBLISH_URL:
            return False, "Login Failed"

        # Shorten the title and content
        if len(title) > 20:
            title = title[:20]
        if len(content) > 1000:
            content = content[:1000]
            
        time.sleep(2)
        tbs = self.drv.find_elements(By.CSS_SELECTOR, ".creator-tab")
        if len(tbs) > 1:
            tbs[2].click()
        time.sleep(4)

        ui = self.drv.find_element(By.CSS_SELECTOR, ".upload-input")
        ui.send_keys('\n'.join(images))
        time.sleep(2)

        ti = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".d-text")))
        ti.send_keys(title)

        if DEBUG:
            print("post, content=", content)


        ci = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".ql-editor")))
        ci.send_keys(content)

        time.sleep(20)
        sbtn = self.drv.find_element(By.CSS_SELECTOR, ".publishBtn")

        sbtn.click()
        if DEBUG:
            print('Story Post Success')
        time.sleep(2)
        return True, "Story post success"

    def register(self, phone_number):
        is_registerred = False
        if DEBUG:
            print("register with phone=", phone_number)
        print("\033[91m⚠️Please input the verification code in Command line, NOT in Browser⚠️\033[0m")
        self.drv.get(LOGIN_URL)
        self._load_cks()
        self.drv.refresh()
        time.sleep(30)
        if self.drv.current_url != LOGIN_URL:
            self._store_cks()
            time.sleep(2)
            is_registerred = True
            if DEBUG:
                print("Register Success with Cookies")
            return is_registerred
        else:
            self.drv.delete_all_cookies()
            if DEBUG:
                print("Clean all the inactive cookies")

        if DEBUG:
            print("Login Manually")
        self.drv.get(LOGIN_URL)
        time.sleep(20)

        pi = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder='手机号']")))
        pi.clear()
        pi.send_keys(phone_number)

        if DEBUG:
            print("Send verification code")
        try:
            scb = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".css-uyobdj")))
            scb.click()
        except:
            try:
                scb = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".css-1vfl29")))
                scb.click()
            except:
                try:
                    scb = self.wait.until(
                        EC.element_to_be_clickable((By.XPATH, "//button[contains(text(),'发送验证码')]")))
                    scb.click()
                except:
                    print("Cannot find the verification code button")

        if DEBUG:
            print("Get verification code")

        for attempt in range(3):
            vc = input(f"\033[32m 🔐 Please enter the correct verification code: \033[0m").strip()
            ci = self.wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[placeholder='验证码']")))
            ci.clear()
            time.sleep(0.2)
            ci.send_keys(Keys.CONTROL + "a")
            ci.send_keys(Keys.DELETE)
            ci.send_keys(vc)

            reg_btn = self.wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".beer-login-btn")))
            reg_btn.click()

            # 等待登录跳转或页面变化
            time.sleep(5)
            if self.drv.current_url != LOGIN_URL:
                print("\033[34m✅ Login Success，save cookies ... \033[0m")
                self._store_cks()
                is_registerred = True
                return is_registerred
            else:
                print("\033[91m❌ verification code error or login failed\033[0m")

        return is_registerred
        


    def exit(self):
        if DEBUG:
            print("Close Browser")
        self.drv.quit()

