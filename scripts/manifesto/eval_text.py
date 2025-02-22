"""
Ensure an AI-generated markdown text adheres to the highest safety standards.
"""

# Standard imports
import sys

from typing import Dict, Tuple

# Library imports
from bs4 import BeautifulSoup
from detoxify import Detoxify
from markdown2 import markdown
from prettytable import PrettyTable

# Constants
TOXICITY_THRESHOLD = 0.005
INSULT_THRESHOLD = 0.005

def markdown_to_text(md_text: str) -> str:
    """Converts Markdown to plain text."""
    html_content = markdown(md_text)
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.get_text()

def dict_to_table(d: Dict, fields: Dict = None) -> str:
    """Convert a dictionary into a pretty table of keys and values"""
    table = PrettyTable()
    if fields is None:
        fields = {
            'key': "Metric",
            'value': "Score"
        }
    try:
        table.field_names = [fields['key'], fields['value']]
    except KeyError as error:
        raise ValueError("Fields must include column names for keys and values") from error
    for k, v in d.items():
        table.add_row([k, v])
    return table

def detect_toxicity(text: str) -> Tuple[float]:
    """Detects toxic, obscene, threatening or insulting content."""
    model = Detoxify('original')
    return model.predict(text)

def evaluate_md_text():
    """Main entrypoint"""
    # Read in a Markdown file
    with open(sys.argv[1], 'r', encoding="utf-8") as f:
        md_text = f.read()

    # Convert Markdown to plain text
    text = markdown_to_text(md_text)

    # Evaluate text
    results = detect_toxicity(text)
    results = {metric: score.astype(float) for metric, score in results.items()}
    print(dict_to_table(results))

if __name__ == '__main__':
    evaluate_md_text()
