from dotenv import load_dotenv

# Load variables from .env into the process environment
load_dotenv()

import utility_bot

def main():

    utility_bot.BOT.run(token=utility_bot.TOKEN)

if __name__ == "__main__":
    main()
