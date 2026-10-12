"""
Create synthetic customer/KYC profile records in Salesforce for the AML Investigation Copilot.

Setup (one-time):
    pip install simple-salesforce faker

You'll need three things from your own Salesforce account (never share these with anyone,
including me — this script runs entirely on your machine):
    1. Your Salesforce username (the email you log in with)
    2. Your Salesforce password
    3. Your Salesforce security token
       (Get one: Salesforce Setup -> your avatar -> Settings -> "Reset My Security Token"
       -> Salesforce emails it to you. If you're on a network Salesforce already trusts,
       sometimes no token is needed, but Developer Edition orgs usually require it.)

Fill in the three values below, or better, set them as environment variables so they
never sit in a plaintext file.
"""

import os
import random

from simple_salesforce import Salesforce

SF_USERNAME = os.environ.get("SF_USERNAME", "")
SF_PASSWORD = os.environ.get("SF_PASSWORD", "")
SF_SECURITY_TOKEN = os.environ.get("SF_SECURITY_TOKEN", "")

sf = Salesforce(username=SF_USERNAME, password=SF_PASSWORD, security_token=SF_SECURITY_TOKEN)

random.seed(42)

FIRST_NAMES = ["James", "Maria", "Wei", "Fatima", "Carlos", "Emma", "Raj", "Olga",
               "Kwame", "Sofia", "Tom", "Priya", "Liu", "Anna", "David"]
LAST_NAMES = ["Smith", "Garcia", "Chen", "Al-Farsi", "Rodriguez", "Johnson", "Patel",
              "Ivanova", "Mensah", "Silva", "Brown", "Kumar", "Wang", "Novak", "Lee"]
OCCUPATIONS = ["Small business owner", "Consultant", "Teacher", "Software engineer",
               "Retail manager", "Accountant", "Freelance contractor", "Real estate agent"]

records = []

for i in range(40):
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    records.append({
        "Name": name,
        "Account_Id__c": f"ACC-S-{i:05d}",
        "Occupation__c": random.choice(OCCUPATIONS),
        "Risk_Rating__c": random.choice(["Medium", "High"]),
        "Is_PEP__c": False,
        "KYC_Last_Reviewed__c": "2026-01-15",
    })

for i in range(30):
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    records.append({
        "Name": name,
        "Account_Id__c": f"ACC-L-{i:05d}",
        "Occupation__c": random.choice(OCCUPATIONS),
        "Risk_Rating__c": "High",
        "Is_PEP__c": (i < 3),
        "KYC_Last_Reviewed__c": "2025-11-02",
    })

for i in range(30):
    name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    records.append({
        "Name": name,
        "Account_Id__c": f"ACC-N-{i:05d}",
        "Occupation__c": random.choice(OCCUPATIONS),
        "Risk_Rating__c": "Low",
        "Is_PEP__c": False,
        "KYC_Last_Reviewed__c": "2026-03-10",
    })

print(f"Prepared {len(records)} customer records. Creating in Salesforce...")

results = sf.bulk.Customer_Profile__c.insert(records, batch_size=50)

success_count = sum(1 for r in results if r["success"])
print(f"Created {success_count} / {len(records)} records successfully.")

failures = [r for r in results if not r["success"]]
if failures:
    print("Some records failed — first failure detail:")
    print(failures[0])