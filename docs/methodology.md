# Methodology

## 1. Data cleaning
- Removed cancelled invoices (invoice numbers starting with `C`).
- Removed rows with non-positive quantity or price.
- Removed non-product codes (e.g. `POST`, `DOT`, `M`, `BANK CHARGES`); valid product codes are 5 digits with an optional letter suffix.

## 2. Warehouse interpretation of the data
- **Order (invoice)** = one picking task.
- **Order line** = one visit to a storage location.
- **Pick frequency** of a SKU = number of order lines containing it.

## 3. ABC classification
- Cumulative share thresholds: A ≤ 80%, B ≤ 95%, C = rest.
- Computed twice (by pick frequency and by revenue) to show that the two classifications differ.
