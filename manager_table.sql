-- Удаляем старый VIEW если есть
DROP VIEW IF EXISTS manager_table;

CREATE VIEW manager_table AS
WITH work_hours AS (
    SELECT
        w.id AS worker_id,
        EXTRACT(DAY FROM wl.work_date) AS day,
        SUM(wl.hours) AS hours,
        wl.work_type
    FROM workers w
    LEFT JOIN work_logs wl ON w.id = wl.worker_id
    GROUP BY w.id, day, wl.work_type
),

-- агрегируем часы по дням (1–31)
pivot_hours AS (
    SELECT
        wl.worker_id,

        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 1 THEN wl.hours ELSE 0 END) AS d1,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 2 THEN wl.hours ELSE 0 END) AS d2,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 3 THEN wl.hours ELSE 0 END) AS d3,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 4 THEN wl.hours ELSE 0 END) AS d4,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 5 THEN wl.hours ELSE 0 END) AS d5,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 6 THEN wl.hours ELSE 0 END) AS d6,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 7 THEN wl.hours ELSE 0 END) AS d7,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 8 THEN wl.hours ELSE 0 END) AS d8,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 9 THEN wl.hours ELSE 0 END) AS d9,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 10 THEN wl.hours ELSE 0 END) AS d10,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 11 THEN wl.hours ELSE 0 END) AS d11,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 12 THEN wl.hours ELSE 0 END) AS d12,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 13 THEN wl.hours ELSE 0 END) AS d13,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 14 THEN wl.hours ELSE 0 END) AS d14,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 15 THEN wl.hours ELSE 0 END) AS d15,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 16 THEN wl.hours ELSE 0 END) AS d16,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 17 THEN wl.hours ELSE 0 END) AS d17,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 18 THEN wl.hours ELSE 0 END) AS d18,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 19 THEN wl.hours ELSE 0 END) AS d19,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 20 THEN wl.hours ELSE 0 END) AS d20,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 21 THEN wl.hours ELSE 0 END) AS d21,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 22 THEN wl.hours ELSE 0 END) AS d22,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 23 THEN wl.hours ELSE 0 END) AS d23,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 24 THEN wl.hours ELSE 0 END) AS d24,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 25 THEN wl.hours ELSE 0 END) AS d25,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 26 THEN wl.hours ELSE 0 END) AS d26,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 27 THEN wl.hours ELSE 0 END) AS d27,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 28 THEN wl.hours ELSE 0 END) AS d28,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 29 THEN wl.hours ELSE 0 END) AS d29,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 30 THEN wl.hours ELSE 0 END) AS d30,
        SUM(CASE WHEN EXTRACT(DAY FROM wl.work_date) = 31 THEN wl.hours ELSE 0 END) AS d31

    FROM work_logs wl
    GROUP BY wl.worker_id
),

-- считаем зарплату по ставкам
salary_calc AS (
    SELECT
        wl.worker_id,
        SUM(
            wl.hours * r.rate_per_hour
        ) AS salary
    FROM work_logs wl
    LEFT JOIN rates r
        ON wl.worker_id = r.worker_id
        AND wl.work_type = r.work_type
    GROUP BY wl.worker_id
),

-- расходы
expenses_sum AS (
    SELECT
        worker_id,
        SUM(amount) AS total_expenses
    FROM expenses
    GROUP BY worker_id
),

-- премии
bonuses_sum AS (
    SELECT
        worker_id,
        SUM(amount) AS total_bonuses
    FROM bonuses
    GROUP BY worker_id
),

-- штрафы
penalties_sum AS (
    SELECT
        worker_id,
        SUM(amount) AS total_penalties
    FROM penalties
    GROUP BY worker_id
)

SELECT
    w.full_name,
    w.chat_id,
    w.active,

    -- дни
    COALESCE(p.d1, 0) AS d1,
    COALESCE(p.d2, 0) AS d2,
    COALESCE(p.d3, 0) AS d3,
    COALESCE(p.d4, 0) AS d4,
    COALESCE(p.d5, 0) AS d5,
    COALESCE(p.d6, 0) AS d6,
    COALESCE(p.d7, 0) AS d7,
    COALESCE(p.d8, 0) AS d8,
    COALESCE(p.d9, 0) AS d9,
    COALESCE(p.d10, 0) AS d10,
    COALESCE(p.d11, 0) AS d11,
    COALESCE(p.d12, 0) AS d12,
    COALESCE(p.d13, 0) AS d13,
    COALESCE(p.d14, 0) AS d14,
    COALESCE(p.d15, 0) AS d15,
    COALESCE(p.d16, 0) AS d16,
    COALESCE(p.d17, 0) AS d17,
    COALESCE(p.d18, 0) AS d18,
    COALESCE(p.d19, 0) AS d19,
    COALESCE(p.d20, 0) AS d20,
    COALESCE(p.d21, 0) AS d21,
    COALESCE(p.d22, 0) AS d22,
    COALESCE(p.d23, 0) AS d23,
    COALESCE(p.d24, 0) AS d24,
    COALESCE(p.d25, 0) AS d25,
    COALESCE(p.d26, 0) AS d26,
    COALESCE(p.d27, 0) AS d27,
    COALESCE(p.d28, 0) AS d28,
    COALESCE(p.d29, 0) AS d29,
    COALESCE(p.d30, 0) AS d30,
    COALESCE(p.d31, 0) AS d31,

    -- финансы
    COALESCE(s.salary, 0) AS base_salary,
    COALESCE(b.total_bonuses, 0) AS bonuses,
    COALESCE(pn.total_penalties, 0) AS penalties,
    COALESCE(e.total_expenses, 0) AS expenses,

    -- итог
    (
        COALESCE(s.salary, 0)
        + COALESCE(b.total_bonuses, 0)
        - COALESCE(pn.total_penalties, 0)
        - COALESCE(e.total_expenses, 0)
    ) AS total,

    -- итог с налогом (13%)
    (
        (
            COALESCE(s.salary, 0)
            + COALESCE(b.total_bonuses, 0)
            - COALESCE(pn.total_penalties, 0)
            - COALESCE(e.total_expenses, 0)
        ) * 0.87
    ) AS total_with_tax

FROM workers w
LEFT JOIN pivot_hours p ON w.id = p.worker_id
LEFT JOIN salary_calc s ON w.id = s.worker_id
LEFT JOIN expenses_sum e ON w.id = e.worker_id
LEFT JOIN bonuses_sum b ON w.id = b.worker_id
LEFT JOIN penalties_sum pn ON w.id = pn.worker_id;