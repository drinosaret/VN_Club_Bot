# The following codes can be used to format your message:

# X# or X#.#
# A 'VNDBID', as we call them. These are numbers starting with a character (c, d, p, r, s, u or v), and are optionally followed by a period and a second number. VNDBIDs will automatically be converted into links to the page on the website. For example, typing 'v4.4' will result in 'v4.4'.
# URL
# Any bare URL will be converted into a link, similar to the VNDBIDs. Example: 'http://vndb.org/' will be formatted as 'link'.
# [b]
# [b]Bold[/b] makes text bold.
# [i]
# [i]Italic[/i] makes text italic.
# [u]
# [u]Underlined[/u] makes text underlined.
# [s]
# [s]Strike[/s] will strike through text.
# [url]
# Create a link, can only be used in the form of [url=link]link title[/url].
# E.g. '[url=/v]List of visual novels[/url] and [url=http://blicky.net/]some external website[/url]' will be displayed as 'List of visual novels and some external website'
# [spoiler]
# The [spoiler]-tag should be used to hide information that could spoil the enjoyment of playing the visual novel for people who haven't done so yet.
# [quote]
# When quoting other people, put the quoted message inside a [quote] .. [/quote] block. Please note that the popular [quote=source]-syntax doesn't work on VNDB. (yet)
# [raw]
# Show off your formatting code skills by putting anything you don't want to have formatted in a [raw] tag. Any of the formatting codes mentioned above are ignored within a [raw] .. [/raw] block.
# [code]
# Similar to [raw], except that the text within the [code] .. [/code] block is formatted in a fixed width font and surrounded by a nice box to keep it separate from the rest of your post.
# There is no [img]-tag and there won't likely ever be one, if you want to include screenshots or other images, please upload them to an external hosting service (e.g. Blicky.net) and link to them in your post.

import re


def replace_url(text: str) -> str:
    # Convert [url=LINK]TEXT[/url] to [TEXT](LINK)
    return re.sub(r"\[url=(.*?)\](.*?)\[/url\]", r"[\2](\1)", text)


def replace_spoiler(text: str) -> str:
    # Convert [spoiler]TEXT[/spoiler] to ||TEXT||
    return re.sub(r"\[spoiler\](.*?)\[/spoiler\]", r"||\1||", text)


def replace_bbcode(text: str) -> str:
    text = replace_url(text)
    text = replace_spoiler(text)
    return text


if __name__ == "__main__":
    test1 = "[From [url=https://jastusa.com/games/jast037/full-metal-daemon-muramasa]JAST USA[/url]]"
    test2 = "[spoiler]This is a spoiler[/spoiler]"
    print(replace_url(test1))
    print(replace_spoiler(test2))
