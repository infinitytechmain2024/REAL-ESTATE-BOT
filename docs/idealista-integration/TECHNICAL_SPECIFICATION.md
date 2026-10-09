# Technical Specification

## 1. ListingSource Protocol

```python
class ListingSource(Protocol):
    name: str
    hosts: frozenset[str]

    def supports(self, task: QueryTask) -> bool: ...
    async def search(self, task: QueryTask, *, limit: int) -> list[SourceListing]: ...
    async def aclose(self) -> None: ...
```

## 2. SourceListing

```python
@dataclass(frozen=True)
class SourceListing:
    url: str
    title: str
    price: float | None = None
    currency: str | None = None
    area_m2: float | None = None
    rooms: int | None = None
    address: str | None = None
    property_type: str | None = None
    deal: str | None = None
    description: str = ""
```

## 3. ApifyIdealistaSource

- Рекомендуемые акторы: `azzouzana/idealista-scraper` или `dz_omar/idealista-scraper-api`
- Вызов только в первом раунде кампании
- Результаты писать через `store.finish_fetch(via="api", layer="api")`
- Текст формировать так, чтобы analysis-worker понимал данные (JSON-LD стиль)

## 4. Error Handling

- `TemporaryApifyError` — можно ретраить / делать fallback
- `PermanentApifyError` — источник считаем временно недоступным, кампанию не роняем

## 5. Database

- Миграция должна расширить допустимые значения `layer` значением `'api'`
- `begin_fetch` должен позволять повторно брать `failed` URL при layer="api"
- Вызовы с via="api" не должны увеличивать счётчики отказов хоста
