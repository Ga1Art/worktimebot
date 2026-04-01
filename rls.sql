-- Включаем RLS
ALTER TABLE workers ENABLE ROW LEVEL SECURITY;
ALTER TABLE work_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE expenses ENABLE ROW LEVEL SECURITY;

-- Доступ на чтение (для Google Sheets)
CREATE POLICY "read workers"
ON workers FOR SELECT
USING (true);

CREATE POLICY "read work_logs"
ON work_logs FOR SELECT
USING (true);

CREATE POLICY "read expenses"
ON expenses FOR SELECT
USING (true);