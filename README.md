# cyprus-gamedev-jobs

Простая база компаний и их вакансий.

## Идея

GameDevMap используется только как одноразовый источник списка компаний. Повторно каждый день его скрапить не нужно.

Рабочая модель состоит из трех CSV-таблиц.

### `data/companies.csv`

Справочник компаний:

- `company_id`
- `company`
- `website`
- `domain`
- `type`
- `city`
- `state`
- `country`

### `data/job_sources.csv`

Для каждой компании фиксируем, где реально живут ее вакансии и как их собирать:

- `company_id`
- `company`
- `status`: `not_checked`, `found`, `not_found`
- `source_url`
- `method`: например `ashby`, `pinpoint`, `api`, `html`, `custom`
- `format`: например `json`, `jsonld`, `html_list`, `html_detail`, `api`
- `notes`

Эта таблица заполняется один раз по мере исследования компаний. Автоматически угадывать источник каждый день не нужно.

### `data/jobs.csv`

Текущие вакансии:

- `company_id`
- `company`
- `source_job_id`
- `title`
- `location`
- `url`

## Текущий процесс

1. `data/studios.csv` хранит исходный снимок GameDevMap.
2. Workflow `Initialize database` один раз создает `companies.csv`, `job_sources.csv` и пустой `jobs.csv`.
3. Для каждой компании находим настоящий источник вакансий и заполняем `job_sources.csv`.
4. После заполнения источников делаем простые сборщики под известные методы.
5. Сборщики обновляют только `jobs.csv`.

## Главное правило

Не угадывать источник вакансий при каждом запуске. Сначала найти и зафиксировать источник для компании, затем использовать известный метод сбора.
