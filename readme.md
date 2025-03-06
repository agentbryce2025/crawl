# Web Crawling Tool for Tariff Updates

The goal is to make a script that uses screenshots and gpt-4o to do multi-step interactions with web pages—like identifying GUI elements, deciding which to interact with, and performing those actions. An example use case would be for each link in the test_sources.json navigating to the site and going through sites to find press releases about tariff updates then returning the links to each release in a json format that we will be able to send out to our customers.

## To-Do List

- [x] Clone the repository and set up project environment
- [x] Create .env file for API key storage
- [x] Create .gitignore to protect sensitive information
- [x] Install required dependencies
- [x] Complete the implementation of typing functionality in main.py
- [x] Add functionality to process multiple sources from test_sources.json
- [x] Implement results storage in JSON format for extracted tariff updates
- [x] Add error handling and retry mechanisms
- [x] Improve logging for better debugging
- [x] Adapt browser initialization for better compatibility
- [x] Test with multiple sources
- [x] Add mock mode for testing without browser dependencies
- [ ] Push updates to the repository

## Usage

1. Ensure you have the required dependencies installed:
   ```
   pip install -r requirements.txt
   ```

2. Set up your .env file with your OpenAI API key

3. Run the script:
   ```
   python main.py
   ```

4. Results will be saved in JSON format for further processing.

