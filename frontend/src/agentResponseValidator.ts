import schemaDocumentGenerated from "./generated/agent-response.schema";
import type { AgentResponse } from "./types";

type JsonSchema = Record<string, unknown>;

interface SchemaDocument {
  root: string;
  schemas: Record<string, JsonSchema>;
}

const schemaDocument = schemaDocumentGenerated as unknown as SchemaDocument;
const supportedKeywords = new Set([
  "$ref",
  "additionalProperties",
  "anyOf",
  "const",
  "enum",
  "format",
  "items",
  "maxItems",
  "maxLength",
  "minItems",
  "minLength",
  "minimum",
  "pattern",
  "properties",
  "prefixItems",
  "propertyNames",
  "required",
  "type",
]);

export function isAgentResponse(value: unknown): value is AgentResponse {
  const validationValue = normalizeAgentResponseForValidation(value);
  return (
    isJsonValue(value, new WeakSet()) &&
    validateSchema(
      schemaDocument.schemas[schemaDocument.root],
      validationValue,
    ) &&
    validatePydanticRefinements(
      validationValue as Record<string, unknown>,
    )
  );
}

function normalizeAgentResponseForValidation(value: unknown): unknown {
  if (!isRecord(value) || !isRecord(value.logical_plan)) {
    return value;
  }
  return {
    ...value,
    logical_plan: camelizeJsonKeys(value.logical_plan),
  };
}

function camelizeJsonKeys(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(camelizeJsonKeys);
  }
  if (!isRecord(value)) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value).map(([key, nested]) => [
      key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase()),
      camelizeJsonKeys(nested),
    ]),
  );
}

function validateSchema(schema: JsonSchema | undefined, value: unknown): boolean {
  if (
    schema === undefined ||
    Object.keys(schema).some((key) => !supportedKeywords.has(key))
  ) {
    return false;
  }

  const reference = schema.$ref;
  if (reference !== undefined) {
    if (typeof reference !== "string") {
      return false;
    }
    const prefix = "#/components/schemas/";
    if (!reference.startsWith(prefix)) {
      return false;
    }
    return validateSchema(schemaDocument.schemas[reference.slice(prefix.length)], value);
  }

  const alternatives = schema.anyOf;
  if (alternatives !== undefined) {
    if (!Array.isArray(alternatives)) {
      return false;
    }
    return alternatives.some(
      (alternative) => isRecord(alternative) && validateSchema(alternative, value),
    );
  }

  if (!validateType(schema.type, value)) {
    return false;
  }
  if ("const" in schema && !Object.is(value, schema.const)) {
    return false;
  }
  if (
    Array.isArray(schema.enum) &&
    !schema.enum.some((item) => Object.is(item, value))
  ) {
    return false;
  }

  if (typeof value === "string" && !validateString(schema, value)) {
    return false;
  }
  if (typeof value === "number" && !validateNumber(schema, value)) {
    return false;
  }
  if (Array.isArray(value) && !validateArray(schema, value)) {
    return false;
  }
  if (isRecord(value) && !validateObject(schema, value)) {
    return false;
  }
  return true;
}

function validateType(type: unknown, value: unknown): boolean {
  if (type === undefined) {
    return true;
  }
  switch (type) {
    case "array":
      return Array.isArray(value);
    case "boolean":
      return typeof value === "boolean";
    case "integer":
      return (
        typeof value === "number" &&
        Number.isFinite(value) &&
        Number.isInteger(value)
      );
    case "null":
      return value === null;
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "object":
      return isRecord(value);
    case "string":
      return typeof value === "string";
    default:
      return false;
  }
}

function validateString(schema: JsonSchema, value: string): boolean {
  if (
    typeof schema.minLength === "number" &&
    value.trim().length < schema.minLength
  ) {
    return false;
  }
  if (typeof schema.maxLength === "number" && value.length > schema.maxLength) {
    return false;
  }
  if (typeof schema.pattern === "string" && !new RegExp(schema.pattern).test(value)) {
    return false;
  }
  if (schema.format === "date-time" && !Number.isFinite(Date.parse(value))) {
    return false;
  }
  if (schema.format !== undefined && schema.format !== "date-time") {
    return false;
  }
  return true;
}

function validateNumber(schema: JsonSchema, value: number): boolean {
  return (
    Number.isFinite(value) &&
    (typeof schema.minimum !== "number" || value >= schema.minimum)
  );
}

function validateArray(schema: JsonSchema, value: unknown[]): boolean {
  if (typeof schema.minItems === "number" && value.length < schema.minItems) {
    return false;
  }
  if (typeof schema.maxItems === "number" && value.length > schema.maxItems) {
    return false;
  }
  if (schema.prefixItems !== undefined) {
    const prefixItems = schema.prefixItems;
    if (!Array.isArray(prefixItems) || value.length !== prefixItems.length) {
      return false;
    }
    return value.every(
      (item, index) =>
        isRecord(prefixItems[index]) &&
        validateSchema(prefixItems[index] as JsonSchema, item),
    );
  }
  if (schema.items !== undefined) {
    if (!isRecord(schema.items)) {
      return false;
    }
    return value.every((item) => validateSchema(schema.items as JsonSchema, item));
  }
  return true;
}

function validateObject(
  schema: JsonSchema,
  value: Record<string, unknown>,
): boolean {
  const properties = schema.properties;
  if (properties !== undefined && !isRecord(properties)) {
    return false;
  }
  const propertySchemas = (properties ?? {}) as Record<string, unknown>;
  const required = schema.required;
  if (
    required !== undefined &&
    (!Array.isArray(required) ||
      !required.every(
        (key) => typeof key === "string" && Object.hasOwn(value, key),
      ))
  ) {
    return false;
  }

  for (const [key, item] of Object.entries(value)) {
    if (schema.propertyNames !== undefined) {
      if (
        !isRecord(schema.propertyNames) ||
        !validateSchema(schema.propertyNames, key)
      ) {
        return false;
      }
    }
    const propertySchema = propertySchemas[key];
    if (propertySchema !== undefined) {
      if (!isRecord(propertySchema) || !validateSchema(propertySchema, item)) {
        return false;
      }
      continue;
    }
    if (schema.additionalProperties === false) {
      return false;
    }
    if (
      isRecord(schema.additionalProperties) &&
      !validateSchema(schema.additionalProperties, item)
    ) {
      return false;
    }
  }
  return true;
}

function validatePydanticRefinements(body: Record<string, unknown>): boolean {
  if (body.ok !== (body.error === null)) {
    return false;
  }
  if (
    body.version_pins !== null &&
    (!isRecord(body.version_pins) ||
      !validateVersionPins(body.version_pins))
  ) {
    return false;
  }
  if (
    !hasUniqueStrings(body.limitations) ||
    !hasUniqueObjectIds(body.analysis_steps, "step_id") ||
    !hasUniqueObjectIds(body.artifacts, "artifact_id") ||
    !hasUniqueObjectIds(body.evidence, "evidence_id")
  ) {
    return false;
  }
  if (!validateChart(body.chart, body.rows)) {
    return false;
  }
  if (body.logical_plan === null) {
    return true;
  }
  if (!isRecord(body.logical_plan)) {
    return false;
  }
  const plan = body.logical_plan;
  return (
    validateFilters(plan.filters) &&
    validateFilters(plan.having) &&
    validateSeriesAxis(plan.seriesAxis) &&
    validateCrossTab(plan.crossTab)
  );
}

function validateChart(chart: unknown, rows: unknown): boolean {
  if (chart === null) {
    return true;
  }
  if (
    !isRecord(chart) ||
    typeof chart.x_field !== "string" ||
    typeof chart.y_field !== "string" ||
    !Array.isArray(rows) ||
    rows.length === 0
  ) {
    return false;
  }
  if (
    !rows.every(
      (row) =>
        isRecord(row) &&
        Object.hasOwn(row, chart.x_field as string) &&
        Object.hasOwn(row, chart.y_field as string),
    )
  ) {
    return false;
  }
  return rows.some((row) => {
    if (!isRecord(row)) {
      return false;
    }
    const value = row[chart.y_field as string];
    if (typeof value === "number") {
      return Number.isFinite(value);
    }
    return (
      typeof value === "string" &&
      value.trim() !== "" &&
      Number.isFinite(Number(value))
    );
  });
}

function validateFilters(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.every((item) => {
      if (!isRecord(item)) {
        return false;
      }
      const isSet = item.operator === "in" || item.operator === "not_in";
      const isNull =
        item.operator === "is_null" || item.operator === "is_not_null";
      if (isNull) {
        return item.value === null;
      }
      if (item.value === null) {
        return false;
      }
      return isSet
        ? Array.isArray(item.value) && item.value.length > 0
        : !Array.isArray(item.value);
    })
  );
}

function validateSeriesAxis(value: unknown): boolean {
  if (value === null) {
    return true;
  }
  if (!isRecord(value)) {
    return false;
  }
  return value.kind === "time"
    ? value.timeGrain !== null
    : value.timeGrain === null;
}

function validateCrossTab(value: unknown): boolean {
  if (value === null) {
    return true;
  }
  if (!isRecord(value) || !Array.isArray(value.values)) {
    return false;
  }
  return (
    value.rowAxis !== value.columnAxis &&
    new Set(value.values).size === value.values.length
  );
}

function hasUniqueComponents(value: unknown): boolean {
  if (!Array.isArray(value)) {
    return false;
  }
  const components = value.map((item) =>
    isRecord(item) ? item.component : undefined,
  );
  return new Set(components).size === components.length;
}

function validateVersionPins(value: Record<string, unknown>): boolean {
  return value.kind === "dataset" && hasUniqueComponents(value.model_versions);
}

function hasUniqueStrings(value: unknown): boolean {
  return (
    Array.isArray(value) &&
    value.every((item) => typeof item === "string") &&
    new Set(value).size === value.length
  );
}

function hasUniqueObjectIds(value: unknown, field: string): boolean {
  if (!Array.isArray(value)) {
    return false;
  }
  const identifiers = value.map((item) => isRecord(item) ? item[field] : undefined);
  return identifiers.every((item) => typeof item === "string") &&
    new Set(identifiers).size === identifiers.length;
}

function isJsonValue(value: unknown, seen: WeakSet<object>): boolean {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return true;
  }
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  if (typeof value !== "object") {
    return false;
  }
  if (seen.has(value)) {
    return false;
  }
  seen.add(value);
  const valid = Array.isArray(value)
    ? value.every((item) => isJsonValue(item, seen))
    : Object.values(value).every((item) => isJsonValue(item, seen));
  seen.delete(value);
  return valid;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
