def add(n1, n2):
  if n1 == "" or n1 is None or n2 == "" or n2 is None:
    return "Invalid Operation"
    
      
  total = int(n1) + int(n2)
  return str(total)  
