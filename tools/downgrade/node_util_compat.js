// Preloaded into unity-cache-server 6.4 (node -r). Its config dependency calls util.is*
// type checks that Node 23 removed; restore them.
const util = require('util');
const checks = {
  isArray: Array.isArray,
  isBoolean: (value) => typeof value === 'boolean',
  isBuffer: Buffer.isBuffer,
  isDate: (value) => value instanceof Date,
  isError: (value) => value instanceof Error,
  isFunction: (value) => typeof value === 'function',
  isNull: (value) => value === null,
  isNullOrUndefined: (value) => value === null || value === undefined,
  isNumber: (value) => typeof value === 'number',
  isObject: (value) => value !== null && typeof value === 'object',
  isPrimitive: (value) => value === null || (typeof value !== 'object' && typeof value !== 'function'),
  isRegExp: (value) => value instanceof RegExp,
  isString: (value) => typeof value === 'string',
  isSymbol: (value) => typeof value === 'symbol',
  isUndefined: (value) => value === undefined,
};
for (const [name, check] of Object.entries(checks)) {
  if (typeof util[name] !== 'function') util[name] = check;
}
