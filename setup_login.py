from playwright.sync_api import sync_playwright
import time

def save_account_cookies():
    with sync_playwright() as p:
        # Launch Chrome with a visible window
        browser = p.chromium.launch(headless=False, channel="chrome") 
        context = browser.new_context()
        page = context.new_page()
        
        print("🔴 NAVIGATING to Facebook... Please look at the Chrome window.")
        page.goto("https://www.facebook.com/")
        
        # Wait for you to log in
        print("waiting for user to log in...")
        print("👉 ACTION REQUIRED: Log in to Facebook manually in the browser window.")
        print("👉 You have 3 minutes.")
        
        # Simple loop to check if you've logged in by looking for the 'Home' icon or similar
        # (Or we just wait a fixed time for simplicity)
        time.sleep(120) 
        
        # Save the cookies to a file
        context.storage_state(path="fb_cookies.json")
        print("✅ SUCCESS! Cookies saved to 'fb_cookies.json'.")
        print("You can close the browser now.")
        browser.close()

if __name__ == "__main__":
    save_account_cookies()
