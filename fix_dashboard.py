#!/usr/bin/env python3
import re

file_path = r'c:\Users\lebron\Downloads\arabela_system\templates\arabela_admin\dashboard.html'

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Find and replace the Monthly Rentals subtitle - first occurrence only
# The pattern includes surrounding context to ensure we're replacing the right one
pattern = r'(Monthly Rentals\s+</h3>\s+<p[^>]*>\s+)Target you.+?ve set for each month(\s+</p>)'

# For Python regex, we need to match the broken apostrophe
# Let's try replacing with a looser regex
content = re.sub(
    r'(Monthly Rentals\s+</h3>\s+<p class="mt-1 text-theme-sm text-gray-500 dark:text-gray-400">\s+)Target you[^m]*ve set for each month',
    r'\1Gowns rented this month',
    content,
    count=1
)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Dashboard updated!")

