# loops_practice.py — try these in the playground!

# 1. Count 1..5
for i in range(1, 6):
    print(i)

# 2. Sum a list
total = 0
for n in [2, 4, 6, 8]:
    total += n
print("total:", total)

# 3. Times table corner
for i in range(1, 4):
    for j in range(1, 4):
        print(i * j, end=" ")
    print()

# 4. While countdown
count = 3
while count > 0:
    print(count)
    count -= 1
print("Go!")
