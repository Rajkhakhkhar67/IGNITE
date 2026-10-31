# variables_examples.py — runnable companion to the
# 'Variables and Data Types' lesson.

name = "Riya"          # str
age = 20                # int
height = 1.62           # float
is_enrolled = True      # bool

print(type(name), type(age), type(height), type(is_enrolled))

# Variables can be reassigned to a new value
streak = 3
streak = streak + 1
print("streak:", streak)

# Naming rules demo
user_name_2 = "valid"    # letters/digits/_ , no leading digit
print(user_name_2)

# Common mistake (uncomment to see the error):
# 2nd_place = "oops"    # SyntaxError: invalid decimal literal
