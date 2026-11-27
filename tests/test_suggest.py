"""Search suggestions in the top bar."""
from app.suggest import norm, score
from test_tools import add


def test_norm_and_score():
    assert norm("Pokémon: Red!") == "pokemon red"
    assert score("final fantasy vii", "Final Fantasy VII") == 0
    assert score("final", "Final Fantasy VII") == 1
    assert score("fant fin", "Final Fantasy VII") == 2          # word starts, any order
    assert score("ff vii", "Final Fantasy VII") == 3            # initials
    assert score("ffvii", "Final Fantasy VII") == 3
    assert score("witcher3", "The Witcher 3") == 4              # spaces ignored
    assert score("pokemon", "Pokémon Red") == 1                 # accents ignored
    assert score("zelda", "Final Fantasy VII") is None
    assert score("", "x") is None


def names(r):
    return [g["title"] for g in r.json["games"]]


def test_suggest_ranks_and_filters(client, dirs):
    _, games = dirs
    for folder, title in (("Hollow Knight v1", "Hollow Knight"), ("The Knight Witch v1", "The Knight Witch"),
                          ("Knights of Pen v1", "Knights of Pen"), ("Celeste v1", "Celeste")):
        add(client, games, folder, title)
    gid = add(client, games, "Kingdom v1", "Kingdom")
    client.post(f"/games/{gid}/edit", {"title": "Kingdom", "platform": "windows", "tags": "knightly, strategy",
                                       "personal": "1", "favorite": "1"})
    r = client.get("/api/suggest?q=knight")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert names(r) == ["Knights of Pen", "Hollow Knight", "The Knight Witch"]
    assert r.json["games"][0]["url"].startswith("/games/") and r.json["games"][0]["platform"] == "Windows"
    assert r.json["games"][0]["latest"] and r.json["games"][0]["status"]
    assert [t["name"] for t in r.json["tags"]] == ["knightly"] and r.json["tags"][0]["url"] == "/?tag=knightly"
    assert r.json["all_url"] == "/?q=knight" and r.json["fuzzy"] is False
    assert names(client.get("/api/suggest?q=hk")) == ["Hollow Knight"]
    assert client.get("/api/suggest?q=king").json["games"][0]["favorite"] is True


def test_suggest_fuzzy_and_empty(client, dirs):
    _, games = dirs
    add(client, games, "Celeste v1", "Celeste")
    r = client.get("/api/suggest?q=celest")
    assert names(r) == ["Celeste"] and not r.json["fuzzy"]
    r = client.get("/api/suggest?q=celsete")                    # typo
    assert names(r) == ["Celeste"] and r.json["fuzzy"]
    assert client.get("/api/suggest?q=zzzzzz").json["games"] == []
    assert client.get("/api/suggest?q=%20!!").json == {"q": "!!", "games": [], "tags": [], "categories": [],
                                                       "fuzzy": False}


def test_suggest_groups_and_categories(client, dirs):
    _, games = dirs
    add(client, games, "Puzzle/Baba Is You v1", "Baba Is You")
    r = client.get("/api/suggest?q=puzz")
    cats = r.json["categories"]
    assert cats and cats[0]["name"] == "Puzzle" and cats[0]["kind"] == "group" and "group=" in cats[0]["url"]


def test_suggest_needs_login(app):
    r = app.test_client().get("/api/suggest?q=a")
    assert r.status_code == 401


def test_search_box_has_suggest(client):
    page = client.get("/").get_data(as_text=True)
    assert 'data-suggest="/api/suggest"' in page and 'role="combobox"' in page
