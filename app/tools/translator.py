from deep_translator import GoogleTranslator
from word2number import w2n
import re

def translate_to_english(text: str):

    try:

        translated = GoogleTranslator(
            source="auto",
            target="en"
        ).translate(text)

        return translated

    except Exception:

        return text
    
def normalize_numbers(text):

    def replace(match):

        word = match.group(0)

        try:
            return str(
                w2n.word_to_num(word)
            )
        except:
            return word

    pattern = (
    r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand)"
    r"(?:\s+(?:and\s+)?(?:zero|one|two|three|four|five|six|seven|eight|nine|"
    r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand))*\b"
)

    return re.sub(
        pattern,
        replace,
        text,
        flags=re.IGNORECASE
    )