const API_BASE_URL = 'https://YOUR_DOMAIN_OR_IP';
const API_KEY = 'YOUR_API_KEY';
const SYNC_ENDPOINT = '/admin-sheet-sync';
const CURRENT_MONTH_SHEET = 'current_month';
const WATCHED_SHEETS = ['Rates', 'Bonuses', 'Penalties', CURRENT_MONTH_SHEET];
const DIRECTORY_SHEET = 'WorkersDirectory';
const DEBOUNCE_SECONDS = 20;
const SYNC_TRIGGER_HANDLER = 'runScheduledAdminSheetSync';
const INSTALLABLE_EDIT_TRIGGER_HANDLER = 'handleSheetEdit';
const LAST_SYNC_PROPERTY = 'admin_sheet_sync_last_run_at';
const LAST_SYNC_STATUS_PROPERTY = 'admin_sheet_sync_last_status';
const LAST_SYNC_ERROR_PROPERTY = 'admin_sheet_sync_last_error';
const LAST_SYNC_STARTED_AT_MS_PROPERTY = 'admin_sheet_sync_last_started_at_ms';
const PENDING_SYNC_PROPERTY = 'admin_sheet_sync_pending';
const IMMEDIATE_SYNC_COOLDOWN_MS = 3000;
const UX_SETUP_ROW_LIMIT = 1000;
const ERROR_BACKGROUND = '#fce8e6';
const NORMAL_BACKGROUND = '#ffffff';

const SHEET_HEADERS = {
  Rates: ['worker_id', 'full_name', 'shift_rate', 'install_rate'],
  Bonuses: ['worker_id', 'full_name', 'bonus_date', 'amount', 'description'],
  Penalties: ['worker_id', 'full_name', 'penalty_date', 'amount', 'description'],
  [CURRENT_MONTH_SHEET]: ['worker_id', 'full_name', 'shift_rate', 'install_rate'],
  WorkersDirectory: ['worker_id', 'full_name'],
};

const HEADER_ALIASES = {
  worker_id: ['worker_id', 'id сотрудника', 'id', 'сотрудник id'],
  full_name: ['full_name', 'фио', 'сотрудник', 'имя сотрудника', 'полное имя'],
  shift_rate: ['shift_rate', 'ставка смена', 'ставка за смену', 'смена ставка'],
  install_rate: ['install_rate', 'ставка монтаж', 'ставка за монтаж', 'монтаж ставка'],
  bonus_date: ['bonus_date', 'дата премии'],
  penalty_date: ['penalty_date', 'дата штрафа'],
  amount: ['amount', 'сумма'],
  description: ['description', 'описание', 'комментарий'],
  project_id: ['project_id', 'id проекта'],
  project_name: ['project_name', 'название проекта', 'проект'],
  participants_count: ['participants_count', 'участников', 'количество участников'],
  total_hours: ['total_hours', 'всего часов', 'часы всего'],
};

const HEADER_NOTES = {
  Rates: {
    worker_id: 'ID сотрудника. Обычно не меняется вручную.',
    full_name: 'Имя сотрудника для удобства. Заполняется автоматически из справочника.',
    shift_rate: 'Почасовая ставка за смену. Допустимы 0 и положительные значения.',
    install_rate: 'Почасовая ставка за монтаж. Допустимы 0 и положительные значения.',
  },
  Bonuses: {
    worker_id: 'ID сотрудника. Выбирайте из списка.',
    full_name: 'Подставляется автоматически по worker_id.',
    bonus_date: 'Дата премии. Форматы: YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY.',
    amount: 'Сумма премии. Должна быть больше 0.',
    description: 'Краткое описание причины премии.',
  },
  Penalties: {
    worker_id: 'ID сотрудника. Выбирайте из списка.',
    full_name: 'Подставляется автоматически по worker_id.',
    penalty_date: 'Дата штрафа. Форматы: YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY.',
    amount: 'Сумма штрафа. Должна быть больше 0.',
    description: 'Краткое описание причины штрафа.',
  },
  [CURRENT_MONTH_SHEET]: {
    worker_id: 'ID сотрудника. Обычно не меняется вручную.',
    full_name: 'Имя сотрудника для удобства.',
    shift_rate: 'Ставка за смену. Можно исправить вручную, затем синхронизировать.',
    install_rate: 'Ставка за монтаж. Можно исправить вручную, затем синхронизировать.',
  },
  WorkersDirectory: {
    worker_id: 'Служебный справочник ID сотрудников.',
    full_name: 'Служебный справочник имён сотрудников.',
  },
};

function syncAdminData() {
  const result = callAdminSheetSync_();
  setupAdminSheetUx_();
  return result;
}

function handleSheetEdit(e) {
  if (!e || !e.range) {
    return;
  }

  const sheet = e.range.getSheet();
  const sheetName = sheet.getName();
  if (!WATCHED_SHEETS.includes(sheetName)) {
    return;
  }

  if (e.range.getRow() === 1) {
    return;
  }

  if (sheetName === 'Rates' || sheetName === CURRENT_MONTH_SHEET) {
    refreshWorkersDirectory_();
  }

  autofillLinkedFields_(sheet, e.range);
  if (sheetName === 'Rates' || sheetName === CURRENT_MONTH_SHEET) {
    mirrorRateRowToSiblingSheet_(sheet, e.range.getRow());
  }
  const isValid = validateRow_(sheet, e.range.getRow());
  if (isValid) {
    runAdminSheetSyncSoon_();
  } else {
    setSyncStatus_('invalid_row', `Row validation failed in ${sheetName}:${e.range.getRow()}`);
  }
}

function runScheduledAdminSheetSync() {
  clearScheduledSyncTriggers_();
  return runAdminSheetSyncSoon_(true);
}

function installAutoSyncTrigger() {
  const spreadsheet = SpreadsheetApp.getActive();
  const hasTrigger = ScriptApp.getProjectTriggers().some(
    trigger =>
      trigger.getHandlerFunction() === INSTALLABLE_EDIT_TRIGGER_HANDLER &&
      trigger.getEventType() === ScriptApp.EventType.ON_EDIT
  );

  if (!hasTrigger) {
    ScriptApp.newTrigger(INSTALLABLE_EDIT_TRIGGER_HANDLER)
      .forSpreadsheet(spreadsheet)
      .onEdit()
      .create();
  }

  setupAdminSheetUx_();
  notify_(hasTrigger ? 'Автосинхронизация уже включена.' : 'Автосинхронизация включена.');
}

function removeAutoSyncTriggers() {
  ScriptApp.getProjectTriggers()
    .filter(
      trigger =>
        trigger.getHandlerFunction() === INSTALLABLE_EDIT_TRIGGER_HANDLER ||
        trigger.getHandlerFunction() === SYNC_TRIGGER_HANDLER
    )
    .forEach(trigger => ScriptApp.deleteTrigger(trigger));

  notify_('Триггеры автосинхронизации удалены.');
}

function getLastAdminSyncAt() {
  return (
    PropertiesService.getScriptProperties().getProperty(LAST_SYNC_PROPERTY) ||
    'never'
  );
}

function getLastAdminSyncDiagnostics() {
  const props = PropertiesService.getScriptProperties();
  return {
    lastRunAt: props.getProperty(LAST_SYNC_PROPERTY) || 'never',
    lastStatus: props.getProperty(LAST_SYNC_STATUS_PROPERTY) || 'unknown',
    lastError: props.getProperty(LAST_SYNC_ERROR_PROPERTY) || '',
  };
}

function setupAdminSheetUx() {
  setupAdminSheetUx_();
  notify_('Справочник сотрудников, подсказки, автозаполнение и валидации обновлены.');
}

function validateAdminSheets() {
  const spreadsheet = SpreadsheetApp.getActive();
  let invalidRows = 0;

  ['Rates', 'Bonuses', 'Penalties', CURRENT_MONTH_SHEET].forEach(sheetName => {
    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      return;
    }

    const lastRow = sheet.getLastRow();
    for (let row = 2; row <= lastRow; row += 1) {
      const isValid = validateRow_(sheet, row);
      if (!isValid) {
        invalidRows += 1;
      }
    }
  });

  if (invalidRows) {
    notify_(`Найдены строки с ошибками: ${invalidRows}. Они подсвечены красным и не будут автосинхронизироваться, пока не будут исправлены.`);
  } else {
    notify_('Ошибок в админских листах не найдено.');
  }
}

function scheduleAdminSheetSync_() {
  setSyncStatus_('scheduled', 'Waiting for debounced admin-sheet-sync run.');
  const triggers = ScriptApp.getProjectTriggers().filter(
    trigger => trigger.getHandlerFunction() === SYNC_TRIGGER_HANDLER
  );
  if (triggers.length > 0) {
    return;
  }

  ScriptApp.newTrigger(SYNC_TRIGGER_HANDLER)
    .timeBased()
    .after(DEBOUNCE_SECONDS * 1000)
    .create();
}

function clearScheduledSyncTriggers_() {
  ScriptApp.getProjectTriggers()
    .filter(trigger => trigger.getHandlerFunction() === SYNC_TRIGGER_HANDLER)
    .forEach(trigger => ScriptApp.deleteTrigger(trigger));
}

function runAdminSheetSyncSoon_(force = false) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(500)) {
    PropertiesService.getScriptProperties().setProperty(PENDING_SYNC_PROPERTY, '1');
    setSyncStatus_('busy', 'Sync already running, marked one pending rerun.');
    return { status: 'busy' };
  }

  let shouldRerun = false;
  let result = { status: 'ok' };
  try {
    const props = PropertiesService.getScriptProperties();
    const now = Date.now();
    const lastStartedAt = Number(props.getProperty(LAST_SYNC_STARTED_AT_MS_PROPERTY) || '0');

    if (!force && now - lastStartedAt < IMMEDIATE_SYNC_COOLDOWN_MS) {
      props.setProperty(PENDING_SYNC_PROPERTY, '1');
      setSyncStatus_('debounced', 'Recent sync already started, marked one pending rerun.');
      result = { status: 'debounced' };
    } else {
      props.setProperty(LAST_SYNC_STARTED_AT_MS_PROPERTY, String(now));
      props.deleteProperty(PENDING_SYNC_PROPERTY);
      SpreadsheetApp.flush();

      result = callAdminSheetSync_();
      setupAdminSheetUx_();
    }
    shouldRerun = props.getProperty(PENDING_SYNC_PROPERTY) === '1';
  } finally {
    lock.releaseLock();
  }

  if (shouldRerun) {
    PropertiesService.getScriptProperties().deleteProperty(PENDING_SYNC_PROPERTY);
    Utilities.sleep(IMMEDIATE_SYNC_COOLDOWN_MS);
    return runAdminSheetSyncSoon_(true);
  }

  return result;
}

function callAdminSheetSync_() {
  const response = UrlFetchApp.fetch(`${API_BASE_URL}${SYNC_ENDPOINT}`, {
    method: 'post',
    headers: {
      'x-api-key': API_KEY,
    },
    muteHttpExceptions: true,
  });

  const responseCode = response.getResponseCode();
  const responseText = response.getContentText();
  PropertiesService.getScriptProperties().setProperty(
    LAST_SYNC_PROPERTY,
    new Date().toISOString()
  );

  if (responseCode >= 200 && responseCode < 300) {
    setSyncStatus_('ok', responseText);
    return JSON.parse(responseText);
  }

  setSyncStatus_('error', `admin-sheet-sync failed (${responseCode}): ${responseText}`);
  throw new Error(`admin-sheet-sync failed (${responseCode}): ${responseText}`);
}


function setupAdminSheetUx_() {
  const spreadsheet = SpreadsheetApp.getActive();
  refreshWorkersDirectory_();
  applyHeaderNotes_(spreadsheet);
  applyDataValidations_(spreadsheet);
  validateAdminSheetsQuietly_();
}

function refreshWorkersDirectory_() {
  const spreadsheet = SpreadsheetApp.getActive();
  const directorySheet = getOrCreateSheet_(spreadsheet, DIRECTORY_SHEET);
  const directoryRows = buildWorkerDirectoryRows_();

  directorySheet.clearContents();
  directorySheet.getRange(1, 1, directoryRows.length, directoryRows[0].length).setValues(directoryRows);
  directorySheet.hideSheet();
}

function buildWorkerDirectoryRows_() {
  const spreadsheet = SpreadsheetApp.getActive();
  const directory = new Map();

  ['Rates', CURRENT_MONTH_SHEET].forEach(sheetName => {
    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      return;
    }

    const values = sheet.getDataRange().getDisplayValues();
    if (!values.length) {
      return;
    }

    const headers = values[0].map(normalizeHeader_);
    const workerIdIndex = headers.indexOf('worker_id');
    const fullNameIndex = headers.indexOf('full_name');
    if (workerIdIndex === -1 || fullNameIndex === -1) {
      return;
    }

    values.slice(1).forEach(row => {
      const workerId = String(row[workerIdIndex] || '').trim();
      const fullName = String(row[fullNameIndex] || '').trim();
      if (workerId && fullName && !directory.has(workerId)) {
        directory.set(workerId, fullName);
      }
    });
  });

  const rows = [['worker_id', 'full_name']];
  [...directory.entries()]
    .sort((a, b) => Number(a[0]) - Number(b[0]))
    .forEach(([workerId, fullName]) => rows.push([workerId, fullName]));

  return rows;
}

function applyHeaderNotes_(spreadsheet) {
  Object.entries(HEADER_NOTES).forEach(([sheetName, notesMap]) => {
    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      return;
    }

    const headers = SHEET_HEADERS[sheetName] || [];
    headers.forEach((header, index) => {
      const note = notesMap[header];
      if (note) {
        sheet.getRange(1, index + 1).setNote(note);
      }
    });
  });
}

function applyDataValidations_(spreadsheet) {
  const ratesSheet = spreadsheet.getSheetByName('Rates');
  const bonusesSheet = spreadsheet.getSheetByName('Bonuses');
  const penaltiesSheet = spreadsheet.getSheetByName('Penalties');
  const currentSheet = spreadsheet.getSheetByName(CURRENT_MONTH_SHEET);
  const directorySheet = spreadsheet.getSheetByName(DIRECTORY_SHEET);

  const workerIdSourceRange = directorySheet
    ? directorySheet.getRange(`A2:A${Math.max(directorySheet.getMaxRows(), UX_SETUP_ROW_LIMIT)}`)
    : null;

  if (workerIdSourceRange) {
    const workerIdRule = SpreadsheetApp.newDataValidation()
      .requireValueInRange(workerIdSourceRange, true)
      .setAllowInvalid(true)
      .setHelpText('Выберите существующий worker_id из справочника сотрудников.')
      .build();

    applyValidationRule_(bonusesSheet, 2, 1, workerIdRule);
    applyValidationRule_(penaltiesSheet, 2, 1, workerIdRule);
  }

  const nonNegativeNumberRule = SpreadsheetApp.newDataValidation()
    .requireNumberGreaterThanOrEqualTo(0)
    .setAllowInvalid(false)
    .setHelpText('Введите число не меньше 0.')
    .build();

  const positiveNumberRule = SpreadsheetApp.newDataValidation()
    .requireNumberGreaterThan(0)
    .setAllowInvalid(false)
    .setHelpText('Введите число больше 0.')
    .build();

  const dateRule = SpreadsheetApp.newDataValidation()
    .requireDate()
    .setAllowInvalid(true)
    .setHelpText('Используйте дату. Поддерживаемые форматы дополнительно проверяются на сервере.')
    .build();

  applyValidationRule_(ratesSheet, 2, 3, nonNegativeNumberRule, 2);
  applyValidationRule_(currentSheet, 2, 3, nonNegativeNumberRule, 2);
  applyValidationRule_(bonusesSheet, 2, 3, dateRule);
  applyValidationRule_(penaltiesSheet, 2, 3, dateRule);
  applyValidationRule_(bonusesSheet, 2, 4, positiveNumberRule);
  applyValidationRule_(penaltiesSheet, 2, 4, positiveNumberRule);
}

function applyValidationRule_(sheet, startRow, startColumn, rule, columnCount = 1) {
  if (!sheet) {
    return;
  }

  const rowCount = Math.max(sheet.getMaxRows() - startRow + 1, UX_SETUP_ROW_LIMIT);
  sheet.getRange(startRow, startColumn, rowCount, columnCount).setDataValidation(rule);
}

function autofillLinkedFields_(sheet, range) {
  const sheetName = sheet.getName();
  if (!['Rates', 'Bonuses', 'Penalties'].includes(sheetName)) {
    return;
  }

  if (range.getColumn() !== 1) {
    return;
  }

  const row = range.getRow();
  const workerId = String(range.getDisplayValue() || '').trim();
  const fullNameCell = sheet.getRange(row, 2);

  if (!workerId) {
    fullNameCell.clearContent();
    return;
  }

  const directory = getWorkerDirectoryMap_();
  const fullName = directory[workerId];
  if (fullName) {
    fullNameCell.setValue(fullName);
  }
}

function mirrorRateRowToSiblingSheet_(sourceSheet, row) {
  if (row <= 1) {
    return;
  }

  const sourceSheetName = sourceSheet.getName();
  if (sourceSheetName !== 'Rates' && sourceSheetName !== CURRENT_MONTH_SHEET) {
    return;
  }

  const targetSheetName = sourceSheetName === 'Rates' ? CURRENT_MONTH_SHEET : 'Rates';
  const spreadsheet = sourceSheet.getParent();
  const targetSheet = spreadsheet.getSheetByName(targetSheetName);
  if (!targetSheet) {
    return;
  }

  const sourceHeaders = getSheetHeaderIndexMap_(sourceSheet);
  const targetHeaders = getSheetHeaderIndexMap_(targetSheet);
  if (!sourceHeaders.worker_id || !sourceHeaders.full_name || !sourceHeaders.shift_rate || !sourceHeaders.install_rate) {
    return;
  }
  if (!targetHeaders.worker_id || !targetHeaders.full_name || !targetHeaders.shift_rate || !targetHeaders.install_rate) {
    return;
  }

  const workerId = String(sourceSheet.getRange(row, sourceHeaders.worker_id).getDisplayValue() || '').trim();
  if (!workerId || !/^\d+$/.test(workerId)) {
    return;
  }

  const fullName = String(sourceSheet.getRange(row, sourceHeaders.full_name).getDisplayValue() || '').trim();
  const shiftRate = sourceSheet.getRange(row, sourceHeaders.shift_rate).getDisplayValue();
  const installRate = sourceSheet.getRange(row, sourceHeaders.install_rate).getDisplayValue();

  const allowCreateTargetRow = targetSheetName !== CURRENT_MONTH_SHEET;
  const targetRow = findOrCreateWorkerRateRow_(targetSheet, targetHeaders, workerId, allowCreateTargetRow);
  if (!targetRow) {
    return;
  }
  targetSheet.getRange(targetRow, targetHeaders.worker_id).setValue(workerId);
  targetSheet.getRange(targetRow, targetHeaders.full_name).setValue(fullName);
  targetSheet.getRange(targetRow, targetHeaders.shift_rate).setValue(shiftRate);
  targetSheet.getRange(targetRow, targetHeaders.install_rate).setValue(installRate);
}

function getSheetHeaderIndexMap_(sheet) {
  const lastColumn = sheet.getLastColumn();
  if (!lastColumn) {
    return {};
  }

  const headers = sheet
    .getRange(1, 1, 1, lastColumn)
    .getDisplayValues()[0]
    .map(normalizeHeader_);
  const indexMap = {};
  headers.forEach((header, index) => {
    if (header) {
      indexMap[header] = index + 1;
    }
  });
  return indexMap;
}

function findOrCreateWorkerRateRow_(sheet, headerMap, workerId, allowCreate = true) {
  const workerIdColumn = headerMap.worker_id;
  const lastRow = Math.max(sheet.getLastRow(), 2);
  const values = sheet.getRange(2, workerIdColumn, Math.max(lastRow - 1, 1), 1).getDisplayValues();

  for (let index = 0; index < values.length; index += 1) {
    if (String(values[index][0] || '').trim() === workerId) {
      return index + 2;
    }
  }

  if (!allowCreate) {
    return null;
  }

  const newRow = lastRow + 1;
  if (sheet.getMaxRows() < newRow) {
    sheet.insertRowsAfter(sheet.getMaxRows(), newRow - sheet.getMaxRows());
  }
  return newRow;
}

function getWorkerDirectoryMap_() {
  const spreadsheet = SpreadsheetApp.getActive();
  const directorySheet = spreadsheet.getSheetByName(DIRECTORY_SHEET);
  const directory = {};

  if (!directorySheet) {
    return directory;
  }

  const values = directorySheet.getDataRange().getDisplayValues();
  values.slice(1).forEach(row => {
    const workerId = String(row[0] || '').trim();
    const fullName = String(row[1] || '').trim();
    if (workerId && fullName) {
      directory[workerId] = fullName;
    }
  });

  return directory;
}

function validateAdminSheetsQuietly_() {
  const spreadsheet = SpreadsheetApp.getActive();
  ['Rates', 'Bonuses', 'Penalties', CURRENT_MONTH_SHEET].forEach(sheetName => {
    const sheet = spreadsheet.getSheetByName(sheetName);
    if (!sheet) {
      return;
    }

    const lastRow = sheet.getLastRow();
    for (let row = 2; row <= lastRow; row += 1) {
      validateRow_(sheet, row);
    }
  });
}

function validateRow_(sheet, row) {
  const values = sheet.getDataRange().getDisplayValues();
  if (!values.length || row > values.length) {
    return true;
  }

  const headers = values[0].map(normalizeHeader_);
  const rowValues = values[row - 1];
  const sheetName = sheet.getName();
  const errors = [];
  const directory = getWorkerDirectoryMap_();

  if (!rowHasAnyData_(rowValues)) {
    clearRowErrorState_(sheet, row, headers.length);
    return true;
  }

  const rowMap = {};
  headers.forEach((header, index) => {
    rowMap[header] = String(rowValues[index] || '').trim();
  });

  const workerId = rowMap.worker_id;
  const isCurrentMonthContinuationRow =
    sheetName === CURRENT_MONTH_SHEET &&
    !workerId &&
    !rowMap.full_name &&
    !rowMap.shift_rate &&
    !rowMap.install_rate;

  if (isCurrentMonthContinuationRow) {
    clearRowErrorState_(sheet, row, headers.length);
    return true;
  }

  if (!workerId) {
    errors.push('Не указан worker_id.');
  } else if (!/^\d+$/.test(workerId)) {
    errors.push('worker_id должен быть целым числом.');
  } else if (sheetName !== CURRENT_MONTH_SHEET && !directory[workerId]) {
    errors.push('worker_id не найден в справочнике сотрудников.');
  }

  if (sheetName === 'Rates' || sheetName === CURRENT_MONTH_SHEET) {
    if (!isNonNegativeNumber_(rowMap.shift_rate)) {
      errors.push('shift_rate должен быть числом не меньше 0.');
    }
    if (!isNonNegativeNumber_(rowMap.install_rate)) {
      errors.push('install_rate должен быть числом не меньше 0.');
    }
  }

  if (sheetName === 'Bonuses') {
    if (!isSupportedDate_(rowMap.bonus_date)) {
      errors.push('bonus_date в неподдерживаемом формате.');
    }
    if (!isPositiveNumber_(rowMap.amount)) {
      errors.push('amount должен быть числом больше 0.');
    }
    if (!rowMap.description) {
      errors.push('description обязателен.');
    }
  }

  if (sheetName === 'Penalties') {
    if (!isSupportedDate_(rowMap.penalty_date)) {
      errors.push('penalty_date в неподдерживаемом формате.');
    }
    if (!isPositiveNumber_(rowMap.amount)) {
      errors.push('amount должен быть числом больше 0.');
    }
    if (!rowMap.description) {
      errors.push('description обязателен.');
    }
  }

  if (errors.length) {
    markRowError_(sheet, row, headers.length, errors);
    return false;
  }

  clearRowErrorState_(sheet, row, headers.length);
  return true;
}

function rowHasAnyData_(rowValues) {
  return rowValues.some(value => String(value || '').trim() !== '');
}

function isNonNegativeNumber_(value) {
  if (value === '') {
    return false;
  }
  const normalized = String(value).replace(',', '.');
  const parsed = Number(normalized);
  return Number.isFinite(parsed) && parsed >= 0;
}

function isPositiveNumber_(value) {
  if (value === '') {
    return false;
  }
  const normalized = String(value).replace(',', '.');
  const parsed = Number(normalized);
  return Number.isFinite(parsed) && parsed > 0;
}

function isSupportedDate_(value) {
  const raw = String(value || '').trim();
  if (!raw) {
    return false;
  }

  if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
    return true;
  }
  if (/^\d{2}\.\d{2}\.\d{4}$/.test(raw)) {
    return true;
  }
  if (/^\d{2}\/\d{2}\/\d{4}$/.test(raw)) {
    return true;
  }
  return !Number.isNaN(Date.parse(raw));
}

function markRowError_(sheet, row, columnCount, errors) {
  const range = sheet.getRange(row, 1, 1, Math.max(columnCount, 1));
  range.setBackground(ERROR_BACKGROUND);
  range.setNote(errors.join('\n'));
}

function clearRowErrorState_(sheet, row, columnCount) {
  const range = sheet.getRange(row, 1, 1, Math.max(columnCount, 1));
  range.setBackground(NORMAL_BACKGROUND);
  range.clearNote();
}

function getOrCreateSheet_(spreadsheet, sheetName) {
  let sheet = spreadsheet.getSheetByName(sheetName);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(sheetName);
  }
  return sheet;
}

function normalizeHeader_(value) {
  const normalized = String(value || '').trim().toLowerCase();
  for (const [canonical, aliases] of Object.entries(HEADER_ALIASES)) {
    if (aliases.includes(normalized)) {
      return canonical;
    }
  }
  return normalized;
}

function notify_(message) {
  try {
    SpreadsheetApp.getUi().alert(message);
  } catch (error) {
    Logger.log(message);
  }
}

function setSyncStatus_(status, details) {
  const props = PropertiesService.getScriptProperties();
  props.setProperty(LAST_SYNC_PROPERTY, new Date().toISOString());
  props.setProperty(LAST_SYNC_STATUS_PROPERTY, status);
  props.setProperty(LAST_SYNC_ERROR_PROPERTY, details || '');
  Logger.log(`[admin-sheet-sync] ${status}: ${details || ''}`);
}
