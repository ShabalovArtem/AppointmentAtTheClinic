# Запис до клініки – частина DPC

Опис проєкту, запуск, результати лабораторних робіт і дослідів обох курсів – у спільному
[README.md](../README.md) у корені репозиторію (розділи «DPC ЛР1», «Досліди»).

Окремий запуск лише частини DPC (з цього каталогу): `docker compose up -d --build api` –
`compose.yaml` підключає `compose.infra.yaml` (postgres, migrate зі спільними `../migrations`) і
`compose.services.yaml`. Опис наданого каркаса – [README-kit.md](README-kit.md).
