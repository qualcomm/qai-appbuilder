import sys
import os
#sys.path.append(os.path.dirname(__file__))
from RedPoster import RedPublisher
from GenieClient import generate_response
from GenieService import GenieServiceLauncher
import subprocess
import os
import signal
import time
import atexit
import psutil
import random
import glob
import sys
import PIL.Image
import builtins
import re

#current location: ai-engine-direct-helper\samples
CURRENT_DIR = os.getcwd()
RED_APP_DIR = os.path.join(CURRENT_DIR, "apps", "StorySeed")
IMAGE_SCRIPT_PATH = os.path.join(CURRENT_DIR, "python", "stable_diffusion_v2_1", "stable_diffusion_v2_1.py")
IMAGE_GENERATED_PATH = os.path.join(CURRENT_DIR, "python", "stable_diffusion_v2_1", "images")
COOKIES_PATH = os.path.join(RED_APP_DIR, "red_cookies.json")

#MODEL_NAME="IBM-Granite-v3.1-8B" 
MODEL_NAME="Qwen2.0-7B-SSD" 


DEBUG = False
PHONE_NUMBER = ""
STORY_COUNT = 0
IS_RED_REGISTERRED = False
MAX_TRY_TIMES = 5

if DEBUG:
    print(f"CURRENT_DIR: {CURRENT_DIR}")
    print(f"RED_APP_DIR: {RED_APP_DIR}")
    print(f"IMAGE_SCRIPT_PATH: {IMAGE_SCRIPT_PATH}")
    print(f"IMAGE_GENERATED_PATH: {IMAGE_GENERATED_PATH}")
    print(f"COOKIES_PATH: {COOKIES_PATH}")
    print(f"MODEL_NAME: {MODEL_NAME}")

# Words table to generate story and image
kids_vocab = [
    # 1. Colors
    ["red", "blue", "green", "yellow", "pink", "orange", "purple", "black", "white", "brown"],

    # 2. Animals
    ["cat", "dog", "cow", "duck", "sheep", "horse", "pig", "lion", "monkey", "elephant"],

    # 3. Foods
    ["apple", "banana", "bread", "cheese", "milk", "egg", "rice", "cake", "juice", "carrot"],

    # 4. Fruits
    ["orange", "grape", "melon", "peach", "pear", "cherry", "kiwi", "pineapple", "mango", "strawberry"],

    # 5. Vegetables
    ["carrot", "potato", "tomato", "onion", "lettuce", "corn", "spinach", "broccoli", "pepper", "cucumber"],

    # 6. Family
    ["mom", "dad", "sister", "brother", "grandma", "grandpa", "baby", "uncle", "aunt", "cousin"],

    # 7. Household
    ["bed", "chair", "table", "lamp", "sofa", "door", "window", "mirror", "clock", "tv"],

    # 8. Toys
    ["ball", "doll", "blocks", "teddy", "puzzle", "car", "truck", "yo-yo", "robot", "kite"],

    # 9. Body Parts
    ["head", "eye", "nose", "mouth", "ear", "hand", "arm", "leg", "foot", "hair"],

    # 10. Weather
    ["sunny", "rainy", "cloudy", "windy", "snowy", "stormy", "foggy", "hot", "cold", "warm"],

    # 11. School
    ["pen", "book", "desk", "bag", "chalk", "board", "ruler", "crayon", "eraser", "glue"],

    # 12. Transport
    ["car", "bus", "bike", "train", "plane", "ship", "truck", "scooter", "subway", "tram"],

    # 13. Nature
    ["tree", "flower", "leaf", "rock", "river", "mountain", "sky", "cloud", "ocean", "star"],

    # 14. Shapes
    ["circle", "square", "triangle", "rectangle", "oval", "diamond", "star", "heart", "cube", "cone"],

    # 15. Numbers
    ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"],

    # 16. Feelings
    ["happy", "sad", "angry", "scared", "excited", "bored", "tired", "surprised", "lonely", "shy"],

    # 17. Verbs
    ["run", "jump", "eat", "sleep", "sit", "stand", "walk", "play", "read", "write"],

    # 18. Clothing
    ["shirt", "pants", "dress", "hat", "shoes", "socks", "jacket", "scarf", "gloves", "shorts"],

    # 19. Rooms
    ["bedroom", "bathroom", "kitchen", "livingroom", "dining", "garage", "hallway", "balcony", "closet", "office"],

    # 20. Holidays
    ["birthday", "Christmas", "Halloween", "Easter", "Thanksgiving", "NewYear", "holiday", "gift", "party", "fireworks"]
]

def is_ibm_model() -> bool:
    return MODEL_NAME == "IBM-Granite-v3.1-8B"
    
def get_ramdom_words():
    group = random.choice(kids_vocab)               
    words = random.sample(group, 4)       
    print("Selected words: ", words)
    words_string = ",".join(words)
    if DEBUG:
        print("words_string: " + words_string)
    return words_string


def get_title_from_words(words_string, max_chars=15):
    words = words_string.split(",")
    random.shuffle(words) 
    
    for count in [3, 2]: 
        candidates = random.sample(words, count)
        phrase = " ".join(candidates)
        if len(phrase) <= max_chars:
            return phrase
    
    return words[0]

def generate_contents_from_model(promote):
    if is_ibm_model():
        try_times_max = 1
    else:
        try_times_max = MAX_TRY_TIMES
    for try_times in range(try_times_max):
        try:
            contents = generate_response(prompt=promote, model=MODEL_NAME, stream=True, is_debug=DEBUG)
            if len(contents) > 0 and contents != "ERROR_TIMEOUT":
                if DEBUG:
                    print(f"Promote: {promote} Contents: {contents}")
                return contents
            elif contents == "ERROR_TIMEOUT":
                genie_service.restart()
                continue
            else:
                print(f"Other Conditions: Contents=||{contents}||")
                continue
        except subprocess.TimeoutExpired:
            print("⏰ Contents generation timed out.")
            genie_service.restart()
            continue
        except Exception as e:
            print(f"❌ Contents generation failed: {e}")
            genie_service.restart()
            continue
    if try_times == MAX_TRY_TIMES:
        print(f"Reach to max try times {MAX_TRY_TIMES} to generate Contents from AI model")
    return "CONTENTS_ERROR"

def contents_filter(contents):
    recontents = contents
    try:
        recontents = recontents.encode().decode("unicode_escape")
    except Exception as e:
        if DEBUG:
            print("decode fail：", e)
            pass
    if recontents != "":
        ps_index = recontents.find("P.S.")
        if ps_index != -1:
            recontents = recontents[:ps_index].strip()
        
        ps_index = recontents.find("Word count:")
        if ps_index != -1:
            recontents = recontents[:ps_index].strip()

        recontents = re.sub(r"\(\d+\s+characters\).*", "", recontents).strip()
        recontents = re.sub(r"\(\d+\s+words\).*", "", recontents).strip()
    return recontents

def generate_story(word):
    print("Story is generating, please wait...")
    PROMOTE="Just generate a short text, containing words {" + word +"}, must within 950 characters, for children. Body only, no title"
    
    story = generate_contents_from_model(PROMOTE)
    if story == "CONTENTS_ERROR" or len(story) == 0:
        return "STORY_FAILED"

    story = contents_filter(story)
    if len(story) > 1000:
        if is_ibm_model():
            print("For IBM model, using the new words to generate story")
            return "STORY_FAILED"
        print("Story is re-generating, please wait...")
        new_promote = "shorten the content {" + story + "} to within 950 characters, preserve words {" + word + "}."
        story = generate_contents_from_model(new_promote)
        if story == "CONTENTS_ERROR" or len(story) == 0:
            return "STORY_FAILED"
        story = contents_filter(story)

    return story


def generate_title(story):
    print("Title is generating, please wait...")
    try:
        promote = "give a title within 20 characters for the content: {" + story + "}."
        title = generate_contents_from_model(promote)
        if len(title) != 0:
            print("\nTitle Generated!")
        title = title.strip('"')
        if len(title) > 20:
            new_promote = "shorten the title {" + title + "} to within 20 characters"
            title = generate_contents_from_model(new_promote)
            title = title.strip('"')
            if len(title) != 0:
                print("\nNew Title Generated!")
        return title
    except subprocess.TimeoutExpired:
        print("⏰ Title generation timed out.")
        return ""
    except Exception as e:
        return ""


def get_image_path(image_dir):
    jpg_files = glob.glob(os.path.join(image_dir, "*.jpg")) + glob.glob(os.path.join(image_dir, "*.JPG"))
    
    if not jpg_files:
        return ""

    latest_file = max(jpg_files, key=os.path.getmtime)
    
    return os.path.abspath(latest_file)

def generate_image(word):
    print("Image is generating, please wait...")
    if not os.path.exists(IMAGE_SCRIPT_PATH):
        print(f"Cannot find the script：{IMAGE_SCRIPT_PATH}")
        return None
    prompt = f"\"Draw a cute and colorful image for children, including elements: {word}\""
    PIL.Image.Image.show = lambda self, title=None: None
    script_filename = os.path.basename(IMAGE_SCRIPT_PATH)
    sys.argv = [
        script_filename, 
        "--prompt", prompt,
    ]
    with open(IMAGE_SCRIPT_PATH, "r", encoding="utf-8") as f:
        code = f.read()
    
    try:
        exec(code, {"__name__": "__main__"})
    except Exception as e:
        print("failed to generate image: ", e)
        image = get_image_path(IMAGE_GENERATED_PATH)
        if image != "":
            print(f"Try to get the previous image: {image}!")
            return image
        return None
    image = get_image_path(IMAGE_GENERATED_PATH)
    if image != "":
        print(f"Image Generated: {image}!")
        return image
    else:
        print(f"image doese not exist")
        return None


def post_to_red(words, story, title, image):
    if DEBUG:
        print(f" words={words}, \n title={title}, \n story=---\n{story}\n---\n image={image}, \n phone={PHONE_NUMBER}, count={STORY_COUNT}\n")
    if image:
        image_name = image
    else:
        image_name = os.path.join(RED_APP_DIR, "empty.png")
    if DEBUG:
        print("image: " + image_name)
    
    test_json = {
        "title": title,
        "content": story,
        "images": [
          image_name,
        ]
    }
    RED_POSETER = RedPublisher()
    global IS_RED_REGISTERRED
    if not IS_RED_REGISTERRED:
        RED_POSETER.register(PHONE_NUMBER)
        IS_RED_REGISTERRED = True
        

    is_post_failed = False
    times = 0
    while (times < MAX_TRY_TIMES):
        try:
            RED_POSETER.post(test_json["title"], test_json["content"], test_json["images"])
            is_post_failed = False
        except Exception as e:
            print("failed to post red: ", e)
            is_post_failed = True
            
        if not is_post_failed:
            print("Story post to RED success")
            break
        else:
            times += 1
            print("Story post to RED falied, retry times = ", times)
    
    if (times == MAX_TRY_TIMES):
        print("Reach to max try times to post red, ignore...")
    if not DEBUG:
        RED_POSETER.exit()
    
def countdown(seconds):
    width = len(str(seconds))
    for remaining in range(seconds, 0, -1):
        print(f"⏳ remaining：{remaining:>{width}} S", end="\r")
        time.sleep(1)
    print("⏰ time's up!         ")


def cleanup():
    """Stop GenieAPIService and its subprocess"""
    print("Stopping Genie Service...")
    global genie_service
    genie_service.terminate()

    print("Service Stopped, exiting...\n")
    

genie_service = GenieServiceLauncher(MODEL_NAME, DEBUG)
if not genie_service.launch():
    print("\033[91m⚠️Genie Service Start failed, exit ...⚠️\033[0m")
    exit()
print("Genie Service Started")


atexit.register(cleanup)

for log_times in range(3):
    print("\033[91m⚠️Please follow the process in Command Line. Don't do any operations in Chrome Browser⚠️\033[0m")

while len(PHONE_NUMBER) != 11:
    PHONE_NUMBER = input("\033[32m Please input your correct phone number: \033[0m ")
PERIOD_TO_POST = 0
while PERIOD_TO_POST <=0:
    PERIOD_TO_POST = int(input("\033[32m Please input the period (in minutes) to post red (Recommended value > 2 minutes): \033[0m")) * 60


if not os.path.exists(COOKIES_PATH):
    if DEBUG:
        print("Cookies does not exist, login first")
    poster = RedPublisher()
    IS_RED_REGISTERRED = poster.register(PHONE_NUMBER)
    if not DEBUG:
        poster.exit()
else:
    time.sleep(5)

if not IS_RED_REGISTERRED and not os.path.exists(COOKIES_PATH):
    print("\033[91m ⚠️Failed to Login into Red, please try again later, Bye...⚠️\033[0m")
    exit()

while True:
    words = get_ramdom_words()
    count = 0
    error_count = 0
    title_re = False
    story = ""
    for attempt in range(5):
        story = generate_story(words)
        count = len(story)
        if count == 0:
            print("\033[91m ⚠️ Story generates failed, try again later⚠️\033[0m")
            genie_service.restart()
            time.sleep(2)
            break
        elif count >= 995 or story == "STORY_FAILED":
            if DEBUG:
                print(f"The generated story is too long {count}, generate a new one")
            words = get_ramdom_words()
            time.sleep(5)
            continue
        else:
            if DEBUG:
                print(f"The generated story is suitable, story len={count}")
            break        
    if count == 0:
        error_count += 1
        if error_count < 10:
            genie_service.restart()
            continue
        else:
            print("\033[91m ⚠️ Falied for many times, exit... ⚠️\033[0m")
            exit()
    time.sleep(2)
    
    if MODEL_NAME == "IBM-Granite-v3.1-8B":
        if DEBUG:
            print("Skip generate title from AI")
        title_re = True
    else:
        title = generate_title(story)
        count = len(title)
        if count == 0 or count > 20:
            print(f"The generated title is not suitable {count}, re-generate later")
            title_re = True
        else:
            print(f"The generated story is suitable, title len={count}")
            title_re = False
    
    time.sleep(2)
    image = generate_image(words)
    
    if title_re:
        title = get_title_from_words(words)
        title = "单词故事：" + title
        print(f"The re-generated title is: {title}")
    
    STORY_COUNT += 1
    post_to_red(words, story, title, image)
    
    print("\033[91m input Ctrl+C to stop \033[0m")
    countdown(PERIOD_TO_POST)

