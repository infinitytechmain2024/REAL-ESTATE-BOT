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
    plot_m2: float | None = None
```

`plot_m2` — подтверждённая площадь участка отдельно от постройки; расширение исходного ТЗ согласовано в DESIGN.md. Foundation реализован в sources/base.py; нормализация внешнего payload — фаза 2.

## 3. ApifyIdealistaSource

- Владелец выбрал `axlymxp/idealista-scraper` (2026-10-10); первоначальные рекомендации azzouzana/dz_omar пересмотрены по PROVIDERS.md. Земельный output и семантика площади требуют проверки до live.
- Вызов только в первом раунде кампании
- Результаты писать через `store.finish_fetch(ticket, PageResult(..., via="api", layer="api"))`; отдельные kwargs via/layer метод не принимает.
- Текст формировать так, чтобы analysis-worker понимал данные (JSON-LD стиль)

## 4. Error Handling

- `TemporaryApifyError` — можно ретраить / делать fallback
- `PermanentApifyError` — источник считаем временно недоступным, кампанию не роняем

## 5. Database

- Миграция 043 расширяет допустимые значения `layer` значением `'api'`; прежние миграции не редактировать.
- `begin_fetch` должен позволять повторно брать `failed` URL при layer="api"
- Вызовы с via="api" не должны увеличивать счётчики отказов хоста
