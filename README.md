# arkadia-mpackage

Publikacja pakietow Mudlet (kalendarze Arkadii) na
[packages.mudlet.org](https://packages.mudlet.org) metoda **trusted publishing**
(OIDC z GitHub Actions, bez zadnych tokenow w repo).

## Jak to dziala

1. Workflow `sync-publish.yml` (uruchamiany recznie: **Actions -> sync-publish
   -> Run workflow**) sprawdza najnowsze release'y repozytoriow zrodlowych
   z listy w `sources.json`.
2. Gdy pojawi sie nowa wersja, workflow pobiera pakiet `.mpackage`, doklada
   pole `created` do `config.lua` (wymog serwisu) i sklada deterministyczny
   wariant (kazda przebudowa daje identyczny SHA-256).
3. Wariant trafia do release'u tego repozytorium, a nastepnie workflow
   publikuje go w serwisie packages.mudlet.org (OIDC; serwis otwiera PR,
   ktory przechodzi ich walidacje, review i auto-merge).
4. Stan postepu zapisywany jest w `state.json` — ponowne uruchomienie bez
   nowych wersji nic nie zmienia (idempotentnosc), a przerwany run wznawia
   sie od ostatniego zakonczonego kroku.

Repozytoria zrodlowe **nie sa modyfikowane w zaden sposob** — sync czyta
wylacznie ich publiczne release'y.

## Pliki

- `sources.json` — lista skanowanych repozytoriow zrodlowych (mpackage,
  repo, nazwa asseta, stala data `created`).
- `state.json` — stan etapow per pakiet (aktualizowany przez workflow).
- `scripts/` — logika synca (Python, wylacznie biblioteka standardowa).
- `tests/` — zestaw testow (unittest, stdlib):

  ```sh
  python3 -m unittest discover -s tests -t . -v
  ```

## Reczne uruchomienie

Zakladka **Actions** -> workflow **sync-publish** -> **Run workflow**.
Opcjonalny input `package` ogranicza run do jednego pakietu (np. `ishtar_cal`)
— przydatne do ponowienia samej publikacji.
