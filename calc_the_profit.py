def profit(info):
  # Calculate the profit per unit and multiply by total inventory
  total_profit = (info["sell_price"] - info["cost_price"]) * info ["inventory"]
  
  # Round to the nearest whole dollar
  return round(total_profit)
