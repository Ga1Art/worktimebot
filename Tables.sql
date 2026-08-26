-- 👷‍♂️ сотрудники
CREATE TABLE workers (
  id SERIAL PRIMARY KEY,
  full_name TEXT NOT NULL,
  chat_id BIGINT UNIQUE,
  is_approved BOOLEAN DEFAULT false,
  active BOOLEAN DEFAULT true,
  is_admin BOOLEAN DEFAULT false
);

-- 💰 ставки (разные для shift/install)
CREATE TABLE rates (
  id SERIAL PRIMARY KEY,
  worker_id INT REFERENCES workers(id) ON DELETE CASCADE,
  work_type TEXT CHECK (work_type IN ('shift', 'install')),
  rate_per_hour NUMERIC
);

-- ⏱ рабочие часы
CREATE TABLE work_logs (
  id SERIAL PRIMARY KEY,
  worker_id INT REFERENCES workers(id) ON DELETE CASCADE,
  work_type TEXT CHECK (work_type IN ('shift', 'install')),
  work_date DATE,
  hours NUMERIC
);

-- 💸 расходы
CREATE TABLE expenses (
  id SERIAL PRIMARY KEY,
  worker_id INT REFERENCES workers(id) ON DELETE CASCADE,
  expense_date DATE,
  amount NUMERIC,
  description TEXT
);

-- ➕ премии
CREATE TABLE bonuses (
  id SERIAL PRIMARY KEY,
  worker_id INT REFERENCES workers(id) ON DELETE CASCADE,
  bonus_date DATE,
  amount NUMERIC,
  description TEXT
);

-- ➖ штрафы
CREATE TABLE penalties (
  id SERIAL PRIMARY KEY,
  worker_id INT REFERENCES workers(id) ON DELETE CASCADE,
  penalty_date DATE,
  amount NUMERIC,
  description TEXT
);