import logging
import sys
from http import client as http_client


def configure_logging(debug: bool) -> None:
    """Logs to stdout; `debug` also turns on DEBUG and the raw HTTP traffic of every request.

    Called once from main(). It used to run in LibrusScraper's constructor, which meant building a
    scraper - in a test, say - reconfigured the root logger.
    """
    logging.basicConfig(
        level=logging.INFO,  # Set the logging level to INFO
        format="%(asctime)s %(levelname)s %(message)s",  # Set the format for log messages
        handlers=[
            logging.StreamHandler(sys.stdout)  # Log to stdout
        ],
    )
    if debug:
        http_client.HTTPConnection.debuglevel = 1
        logging.getLogger().setLevel(logging.DEBUG)
        requests_log = logging.getLogger("requests.packages.urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True
