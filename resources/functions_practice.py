# functions_practice.py — finish each function, then run.

def square(n):
    return n * n

def greet(name, greeting="Hello"):
    return f"{greeting}, {name}? No wait — {greeting}, {name}."

def min_max(nums):
    return min(nums), max(nums)

def count_passing(marks, threshold=50):
    passed = 0
    for m in marks:
        if m >= threshold:
            passed += 1
    return passed


if __name__ == "__main__":
    print(square(6))
    print(greet("Riya"))
    low, high = min_max([4, 8, 15, 16])
    print(low, high)
    print(count_passing([45, 82, 91]))
