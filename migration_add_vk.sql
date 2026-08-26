-- играция: добавляем vk_id и vk_registration_status в таблицу workers
ALTER TABLE workers ADD COLUMN vk_id BIGINT UNIQUE;
ALTER TABLE workers ADD COLUMN vk_registration_status TEXT DEFAULT 'pending';

-- бновляем существующих пользователей
UPDATE workers SET vk_registration_status = 'approved' WHERE is_approved = true;
UPDATE workers SET vk_registration_status = 'rejected' WHERE is_approved = false AND is_approved IS NOT NULL;
