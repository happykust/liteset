# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Sales data generator for OLTP workloads."""

from typing import Any

from .base import DataGenerator


class SalesGenerator(DataGenerator):
    """
    Generates sales/transaction data.
    Target: 10M+ rows for PostgreSQL.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.product_count = 10000
        self.user_count = 1000000
        self.region_count = 50
        self.category_count = 100

        self.payment_methods = [
            "credit_card",
            "debit_card",
            "paypal",
            "bank_transfer",
            "cash",
            "apple_pay",
            "google_pay",
        ]
        self.payment_weights = [35, 25, 15, 10, 5, 5, 5]

        self.statuses = ["completed", "pending", "processing", "refunded", "cancelled"]
        self.status_weights = [70, 10, 10, 5, 5]

        self._sale_id = 0

    def generate_row(self) -> dict[str, Any]:
        """Generate a single sales row."""
        self._sale_id += 1

        quantity = self.random_int(1, 10)
        unit_price = self.random_float(5, 500)
        discount = self.random_float(0, 0.3) if self.random_bool(0.2) else 0
        amount = round(quantity * unit_price * (1 - discount), 2)

        return {
            "sale_id": self._sale_id,
            "product_id": self.random_int(1, self.product_count),
            "user_id": self.random_int(1, self.user_count),
            "amount": amount,
            "quantity": quantity,
            "discount": round(discount * 100, 2),
            "sale_date": self.random_timestamp().isoformat(),
            "region_id": self.random_int(1, self.region_count),
            "category_id": self.random_int(1, self.category_count),
            "payment_method": self.weighted_choice(
                self.payment_methods, self.payment_weights
            ),
            "status": self.weighted_choice(self.statuses, self.status_weights),
        }

    def get_schema(self) -> dict[str, str]:
        """Get PostgreSQL schema for sales table."""
        return {
            "sale_id": "BIGSERIAL PRIMARY KEY",
            "product_id": "INTEGER NOT NULL",
            "user_id": "INTEGER NOT NULL",
            "amount": "DECIMAL(12, 2) NOT NULL",
            "quantity": "INTEGER DEFAULT 1",
            "discount": "DECIMAL(5, 2) DEFAULT 0",
            "sale_date": "TIMESTAMP WITH TIME ZONE",
            "region_id": "INTEGER",
            "category_id": "INTEGER",
            "payment_method": "VARCHAR(50)",
            "status": "VARCHAR(20)",
        }
