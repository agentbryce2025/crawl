#!/usr/bin/env python3
import os
import sys
import json
import time
import logging
import base64
import re
from threading import local, Lock

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from dotenv import load_dotenv
from openai import OpenAI
from selenium.common.exceptions import NoSuchElementException, TimeoutException

# -------------------------------
# Setup logging and environment
# -------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logging.error("OPENAI_API_KEY not set in environment!")
    sys.exit(1)

# Create an OpenAI client using the new SDK style.
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------------
# Thread-local browser initialization
# -------------------------------
thread_local = local()
browser_init_lock = Lock()

# -------------------------------
# Browser Class Definition
# -------------------------------
class Browser:
    def __init__(self):
        options = uc.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.36 Safari/537.36")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--window-size=1920,1080")
        self.driver = uc.Chrome(options=options, use_chromedriver_temp_dir=False)
        self.wait_time = 10

    def go_to_url(self, url: str, retries=3) -> bool:
        for attempt in range(retries):
            try:
                self.driver.get(url)
                WebDriverWait(self.driver, self.wait_time).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                current_url = self.driver.current_url
                logging.info(f"Navigated to {url}, current URL: {current_url}")
                if current_url.startswith("data:") or "404" in self.driver.title.lower():
                    logging.warning(f"Navigation to {url} resulted in an invalid page.")
                    return False
                return True
            except Exception as e:
                logging.error(f"Attempt {attempt+1}/{retries} failed for {url}: {e}")
                time.sleep(2 ** attempt)
        return False

    def get_page_source(self) -> str:
        time.sleep(5)  # Wait briefly to allow dynamic content to load.
        return self.driver.page_source

    def capture_screenshot(self) -> bytes:
        """Return a screenshot as PNG bytes."""
        return self.driver.get_screenshot_as_png()

    def quit(self):
        try:
            self.driver.quit()
        except Exception as e:
            logging.error(f"Error during driver.quit(): {e}")

    def click_element(self, xpath: str) -> bool:
        """Click an element based on its XPath."""
        try:
            element = WebDriverWait(self.driver, self.wait_time).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            element.click()
            logging.info(f"Clicked element with XPath: {xpath}")
            return True
        except (NoSuchElementException, TimeoutException) as e:
            logging.error(f"Failed to click element with XPath {xpath}: {e}")
            return False

def get_thread_browser():
    if not hasattr(thread_local, "browser"):
        with browser_init_lock:
            thread_local.browser = Browser()
    return thread_local.browser

# -------------------------------
# Tariff Update Extraction Function
# -------------------------------
def extract_tariff_updates(html: str, market: str) -> list:
    """
    Uses gpt-4o (text-only) to extract tariff updates from the provided HTML.
    Each update is expected to have keys: "date", "title", "summary", and "link".
    Returns a JSON array of update objects.
    """
    prompt = f"""
Extract the latest tariff updates for the market '{market}' from the HTML content provided below.
Each update must be represented as a JSON object with the following keys:
  - "date": the date of the announcement (in a standard date format),
  - "title": the title of the update,
  - "summary": a brief summary of the announcement,
  - "link": a direct URL to the source or announcement.
Return the results as a JSON array. If no updates are found, return an empty JSON array.
It is extremely important that the output is valid JSON.

HTML Content:
{html}
"""
    messages = [
        {"role": "system", "content": "You are a helpful assistant that ALWAYS responds in valid JSON. You extract tariff update information from HTML."},
        {"role": "user", "content": prompt}
    ]
    logging.info("Sending prompt to gpt-4o for tariff update extraction.")
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            timeout=60
        )
        # Access the response (a pydantic model) to get the message content.
        ai_output = response.choices[0].message.content
        logging.info(f"gpt-4o extraction response: {ai_output}")
        try:
            # Use regex to find the JSON part of the response
            match = re.search(r"(\[.*\])", ai_output, re.DOTALL)  # Corrected regex
            if match:
                json_string = match.group(1)
                updates = json.loads(json_string)
                return updates
            else:
                logging.warning("No JSON found in GPT-4o output.")
                return []
        except json.JSONDecodeError as e:
            logging.error(f"JSONDecodeError: {e}.  GPT-4o Output: {ai_output}")
            return []
    except Exception as e:
        logging.error(f"Error during tariff update extraction: {e}")
        return []

# -------------------------------
# (Optional) AI Action Analysis Functions
# -------------------------------
def analyze_page_for_action(html: str, screenshot_png: bytes) -> dict:
    """
    Uses gpt-4o to analyze both HTML and screenshot and determine the next action.
    Returns a JSON object with:
      - "action": "click" or "type"
      - "xpath": XPath to target element
      - "text": text to type if applicable
      - "description": explanation of the decision.
    If no action is determined, returns {}.
    """
    try:
        image_b64 = base64.b64encode(screenshot_png).decode('utf-8')
        messages = [
            {"role": "system", "content": (
                "You are a web automation agent controlling a browser. You are given both the HTML of the page and a screenshot (encoded in base64). "
                "Analyze them and decide the next action to take to find tariff updates. Return a JSON object with keys: "
                "'action' (either 'click' or 'type'), 'xpath' (the XPath of the target element), "
                "'text' (if action is 'type', else omit), and 'description' explaining your decision. "
                "If you can extract tariff updates based on the current HTML, return a JSON array of updates instead, using keys 'date', 'title', 'summary', and 'link' for each update. It is extremely important that the output is valid JSON. If no action can be determined, return an empty JSON object: `{}`."
            )},
            {"role": "user", "content": f"HTML:\n{html[:4000]}\n\n(HTML truncated for brevity.)"},  # Increased HTML limit
            {"role": "user", "content": "[IMAGE]", "image_data": image_b64}
        ]
        logging.info("Sending prompt to gpt-4o with HTML and screenshot.")
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            timeout=60
        )
        ai_output = response.choices[0].message.content
        logging.info(f"gpt-4o response: {ai_output}")
        try:
            # Use regex to find the JSON part of the response (action or updates)
            match = re.search(r"(\[.*\]|\{.*\})", ai_output, re.DOTALL)  # Matches arrays and objects
            if match:
                json_string = match.group(1)
                action_obj = json.loads(json_string)
                return action_obj
            else:
                logging.warning("No JSON found in GPT-4o output for action analysis.")
                return {}

        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse gpt-4o output as JSON. Error: {e}, Output: {ai_output}")
            return {}
    except Exception as e:
        logging.error(f"Error during gpt-4o call: {e}")
        return {}

def analyze_page_for_action_html_only(html: str) -> dict:
    """
    Fallback analysis using HTML only via gpt-4o.
    """
    try:
        messages = [
            {"role": "system", "content": (
                "You are a web automation agent controlling a browser. You are given the HTML of the page. "
                "Analyze it and decide the next action to take to find tariff updates. Return a JSON object with keys: "
                "'action' (either 'click' or 'type'), 'xpath' (the XPath of the target element), "
                "'text' (if action is 'type', else omit), and 'description' explaining your decision. "
                "If you can extract tariff updates based on the current HTML, return a JSON array of updates instead, using keys 'date', 'title', 'summary', and 'link' for each update. It is extremely important that the output is valid JSON. If no action can be determined, return an empty JSON object: `{}`."
            )},
            {"role": "user", "content": f"HTML:\n{html[:4000]}\n\nWhat action should be taken next?"}
        ]
        logging.info("Sending HTML-only prompt to gpt-4o for fallback analysis.")
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            timeout=60
        )
        ai_output = response.choices[0].message.content
        logging.info(f"gpt-4o (HTML-only) response: {ai_output}")
        try:
            # Use regex to find the JSON part of the response (action or updates)
            match = re.search(r"(\[.*\]|\{.*\})", ai_output, re.DOTALL)  # Matches arrays and objects
            if match:
                json_string = match.group(1)
                action_obj = json.loads(json_string)
                return action_obj
            else:
                logging.warning("No JSON found in GPT-4o output for HTML-only action analysis.")
                return {}
        except json.JSONDecodeError as e:
            logging.error(f"Failed to parse gpt-4o (HTML-only) output as JSON. Error: {e}, Output: {ai_output}")
            return {}
    except Exception as e:
        logging.error(f"Error during HTML-only gpt-4o call: {e}")
        return {}

# -------------------------------
# Process a Tariff Source
# -------------------------------
def process_tariff_source(source: dict):
    market = source.get("market")
    url = source.get("link")
    logging.info(f"Processing market: {market} at {url}")
    browser = get_thread_browser()
    try:
        if not browser.go_to_url(url):
            logging.error("Failed to load URL.")
            return

        # Main loop for interaction and extraction
        max_iterations = 10  # Limit to prevent infinite loops
        for i in range(max_iterations):
            logging.info(f"Iteration {i+1} of interaction loop.")

            # Capture state: HTML and screenshot
            html = browser.get_page_source()
            screenshot = browser.capture_screenshot()

            # Analyze page for action
            action_obj = analyze_page_for_action(html, screenshot)

            if not action_obj:
                logging.info("No action determined with screenshot, attempting HTML-only analysis.")
                action_obj = analyze_page_for_action_html_only(html)

            if not action_obj:
                logging.info("No action determined after HTML-only analysis.  Stopping.")
                break  # Stop if no action can be determined.

            if isinstance(action_obj, list):  # GPT-4o returned updates directly
                logging.info("Tariff updates extracted directly by GPT-4o.")
                updates = action_obj
                print(json.dumps(updates, indent=4))
                return  # Exit the loop

            action = action_obj.get("action")
            xpath = action_obj.get("xpath")
            text = action_obj.get("text", "")
            description = action_obj.get("description", "No description")

            logging.info(f"Action determined: {action}, XPath: {xpath}, Text: {text}, Description: {description}")

            if action == "click":
                if not browser.click_element(xpath):
                    logging.error(f"Failed to click element, stopping.")
                    break
                time.sleep(3)  # Give the page time to load after the click.
            elif action == "type":
                # TODO: Implement typing into the element (requires finding the element and sending keys)
                logging.warning("Typing action not yet implemented.")
                break # stop further action if typing is needed
            else:
                logging.warning(f"Unknown action: {action}")
                break
        else:
            logging.warning("Maximum iterations reached. Extraction incomplete.")

    finally:
        browser.quit()

# -------------------------------
# Main Entry Point
# -------------------------------
if __name__ == "__main__":
    # Define your test tariff source: Turkmenistan Customs news page.
    test_source = {
        "market": "Turkmenistan - TM",
        "link": "https://customs.gov.tm/news/customs"
    }
    process_tariff_source(test_source)
