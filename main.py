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
def setup_logging(log_file="crawl.log", level=logging.INFO):
    """
    Set up logging to both console and file.
    
    Args:
        log_file (str): Path to the log file.
        level (int): Logging level.
    """
    # Create logs directory if it doesn't exist
    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Clear any existing handlers
    if root_logger.handlers:
        root_logger.handlers = []
    
    # Create file handler
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_format = logging.Formatter('%(asctime)s [%(threadName)s] [%(levelname)s] %(message)s')
    file_handler.setFormatter(file_format)
    root_logger.addHandler(file_handler)
    
    # Create console handler
    console_handler = logging.StreamHandler()
    console_format = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    console_handler.setFormatter(console_format)
    root_logger.addHandler(console_handler)
    
    logging.info(f"Logging configured to file: {log_file}")

# Set up logging
setup_logging(log_file="logs/crawl.log")
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
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        
        # Standard Selenium approach (more reliable in various environments)
        try:
            logging.info("Initializing browser with standard Selenium")
            options = Options()
            options.add_argument("--headless")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.6998.36 Safari/537.36")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--window-size=1920,1080")
            
            # Find Chrome binary
            import subprocess
            try:
                chrome_path = subprocess.check_output(["which", "google-chrome"], text=True).strip()
                logging.info(f"Found Chrome at: {chrome_path}")
                options.binary_location = chrome_path
            except subprocess.CalledProcessError:
                logging.warning("Could not find google-chrome, using default browser location")
            
            self.driver = webdriver.Chrome(options=options)
            self.wait_time = 10
            logging.info("Chrome browser initialized successfully with Selenium")
        except Exception as e:
            logging.error(f"Failed to initialize browser with Selenium: {e}")
            
            # As a last resort, try undetected_chromedriver
            try:
                logging.info("Trying with undetected_chromedriver as fallback")
                options = uc.ChromeOptions()
                options.add_argument("--headless")
                options.add_argument("--disable-gpu")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                self.driver = uc.Chrome(options=options, use_subprocess=True)
                self.wait_time = 10
                logging.info("Browser initialized successfully with undetected_chromedriver")
            except Exception as e2:
                logging.error(f"All browser initialization attempts failed: {e2}")
                raise

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

def get_thread_browser(max_retries=3):
    """
    Gets or creates a thread-specific browser instance with retry mechanism.
    
    Args:
        max_retries (int): Maximum number of retry attempts if initialization fails.
        
    Returns:
        Browser: A browser instance.
    """
    if not hasattr(thread_local, "browser"):
        with browser_init_lock:
            for attempt in range(max_retries):
                try:
                    thread_local.browser = Browser()
                    logging.info("Browser initialized successfully")
                    break
                except Exception as e:
                    logging.error(f"Browser initialization attempt {attempt+1}/{max_retries} failed: {e}")
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(2)
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
        max_iterations = 5  # Limit to prevent infinite loops (reduced for testing)
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
                # Save the updates to file
                save_results(market, updates)
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
                try:
                    element = WebDriverWait(browser.driver, browser.wait_time).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    element.clear()  # Clear any existing text
                    element.send_keys(text)
                    logging.info(f"Typed '{text}' into element with XPath: {xpath}")
                    time.sleep(2)  # Give time for the page to respond
                except (NoSuchElementException, TimeoutException) as e:
                    logging.error(f"Failed to type into element with XPath {xpath}: {e}")
                    break
            else:
                logging.warning(f"Unknown action: {action}")
                break
        else:
            logging.warning("Maximum iterations reached. Extraction incomplete.")

    finally:
        browser.quit()

# -------------------------------
# Results storage function
# -------------------------------
def save_results(market: str, updates: list, output_dir="results"):
    """
    Saves the extracted tariff updates for a market to a JSON file.
    
    Args:
        market (str): The market identifier.
        updates (list): The list of tariff updates.
        output_dir (str): Directory to save results.
    """
    os.makedirs(output_dir, exist_ok=True)
    safe_market = re.sub(r'[^a-zA-Z0-9_-]', '_', market)  # Make filename safe
    output_file = os.path.join(output_dir, f"{safe_market}_updates.json")
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(updates, f, indent=2, ensure_ascii=False)
    
    logging.info(f"Saved {len(updates)} tariff updates for {market} to {output_file}")

# -------------------------------
# Mock Functions for Testing
# -------------------------------
def mock_process_source(source: dict):
    """
    Mocks the processing of a source without requiring a browser.
    Useful for testing the extraction and storage parts of the code.
    
    Args:
        source (dict): The source to process.
    """
    market = source.get("market")
    url = source.get("link")
    logging.info(f"Mock processing source: {market} at {url}")
    
    # Generate mock tariff updates (for testing only)
    mock_updates = [
        {
            "date": "2025-03-01",
            "title": f"Updates to import duties for {market}",
            "summary": "New tariff rates for various commodities imported into the country.",
            "link": f"{url}/tariff-updates-2025"
        },
        {
            "date": "2025-02-15",
            "title": "Changes to customs procedures",
            "summary": "Simplified procedures for customs declarations and documentation requirements.",
            "link": f"{url}/customs-procedures"
        }
    ]
    
    # Save the mock updates
    save_results(market, mock_updates)
    print(json.dumps(mock_updates, indent=4))
    logging.info(f"Saved mock updates for {market}")
    return mock_updates

# -------------------------------
# Main Entry Point
# -------------------------------
if __name__ == "__main__":
    # Create results directory if it doesn't exist
    results_dir = "results"
    os.makedirs(results_dir, exist_ok=True)
    
    # Check if we should use mock mode (no browser)
    use_mock = "--mock" in sys.argv or os.environ.get("USE_MOCK") == "1"
    
    if use_mock:
        logging.info("Running in mock mode (no browser required)")
    
    # Load sources from JSON file
    try:
        with open("test_sources.json", 'r', encoding='utf-8') as f:
            sources = json.load(f)
        logging.info(f"Loaded {len(sources)} sources from test_sources.json")
        
        # Filter active sources (status=1)
        active_sources = [s for s in sources if s.get("status") == "1"]
        logging.info(f"Found {len(active_sources)} active sources to process")
        
        # Process a limited number of sources for testing
        max_sources = 2  # Limit for testing purposes
        for i, source in enumerate(active_sources[:max_sources]):
            market = source.get("market")
            url = source.get("link")
            
            if not url or url.startswith("http") is False:
                logging.warning(f"Skipping source {market} with invalid URL: {url}")
                continue
                
            logging.info(f"Processing source {i+1}/{min(max_sources, len(active_sources))}: {market}")
            
            try:
                if use_mock:
                    # Use mock processing
                    mock_process_source(source)
                else:
                    # Process the source with real browser
                    process_tariff_source(source)
            except Exception as e:
                logging.error(f"Failed to process source {market}: {e}")
        
        logging.info("Processing complete")
        
    except Exception as e:
        logging.error(f"Failed to load or process sources: {e}")
        
    # Single test case for debugging
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        test_source = {
            "market": "Turkmenistan - TM",
            "link": "https://customs.gov.tm/news/customs"
        }
        if use_mock:
            mock_process_source(test_source)
        else:
            process_tariff_source(test_source)
